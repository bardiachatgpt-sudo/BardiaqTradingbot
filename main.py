import os
import re
import json
import time
import uuid
import logging
import threading
from datetime import datetime, timezone, timedelta

import numpy as np
import pandas as pd
import pytz
import websocket
import telebot
from flask import Flask, jsonify


# ============================================================
# CONFIGURATION
# ============================================================

PORT = int(os.getenv("PORT", "8080"))
HOST = os.getenv("HOST", "0.0.0.0")

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", os.getenv("BOT_TOKEN", "")).strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", "30"))
TV_TIMEOUT = int(os.getenv("TV_TIMEOUT", "30"))
TV_IDLE_TIMEOUT = float(os.getenv("TV_IDLE_TIMEOUT", "4"))

STATE_FILE = os.getenv("STATE_FILE", "scanner_state.json")
PRICE_MATCH_WINDOW = int(os.getenv("PRICE_MATCH_WINDOW", "4"))

TEHRAN = pytz.timezone("Asia/Tehran")

SYMBOL = "XAUUSD"

TIMEFRAMES = {
    "M1": {
        "interval": "1",
        "bars": 1440,
        "seconds": 60,
    },
    "M5": {
        "interval": "5",
        "bars": 150,
        "seconds": 300,
    },
}

TV_SOURCES = [
    "OANDA:XAUUSD",
    "FOREXCOM:XAUUSD",
    "FX_IDC:XAUUSD",
]

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(message)s",
)

log = logging.getLogger("macd-divergence-scanner")

app = Flask(__name__)

bot = telebot.TeleBot(BOT_TOKEN, threaded=True) if BOT_TOKEN else None

state_lock = threading.RLock()

subscribed_users = set()
event_history = []
seen_event_ids = set()

source_locks = {}

latest_data = {}
latest_errors = {}

scanner_started = False


# ============================================================
# STATE MANAGEMENT
# ============================================================

def load_state():
    global subscribed_users
    global event_history
    global seen_event_ids

    try:
        if not os.path.exists(STATE_FILE):
            return

        with open(STATE_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)

        subscribed_users = set(data.get("users", []))
        event_history = data.get("events", [])

        seen_event_ids = {
            item.get("id")
            for item in event_history
            if item.get("id")
        }

    except Exception:
        log.exception("State loading failed")


def save_state():
    try:
        with state_lock:
            payload = {
                "users": list(subscribed_users),
                "events": event_history[-1000:],
            }

            temporary = STATE_FILE + ".tmp"

            with open(temporary, "w", encoding="utf-8") as file:
                json.dump(
                    payload,
                    file,
                    ensure_ascii=False,
                    indent=2,
                )

            os.replace(temporary, STATE_FILE)

    except Exception:
        log.exception("State saving failed")


load_state()


# ============================================================
# TIME AND PRICE FORMATTING
# ============================================================

def utc_now():
    return datetime.now(timezone.utc)


def to_tehran(value):
    timestamp = pd.Timestamp(value)

    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize("UTC")

    return timestamp.tz_convert(TEHRAN)


def format_time(value):
    try:
        return to_tehran(value).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(value)


def format_price(value, symbol=SYMBOL):
    digits = 2 if symbol.endswith("JPY") else 4
    return f"{float(value):.{digits}f}"


# ============================================================
# TELEGRAM
# ============================================================

def send_message(message, chat_id=None):
    if not bot:
        log.info("ALERT: %s", message)
        return

    targets = []

    if chat_id is not None:
        targets.append(chat_id)
    else:
        if TELEGRAM_CHAT_ID:
            targets.append(TELEGRAM_CHAT_ID)

        with state_lock:
            targets.extend(list(subscribed_users))

    targets = list(dict.fromkeys(targets))

    for target in targets:
        try:
            bot.send_message(
                target,
                message,
                disable_web_page_preview=True,
            )
        except Exception:
            log.exception("Telegram message failed")


def broadcast(message):
    send_message(message)


if bot:

    @bot.message_handler(commands=["start", "subscribe"])
    def telegram_start(message):
        with state_lock:
            subscribed_users.add(message.chat.id)
            save_state()

        send_message(
            "عضویت شما در اسکنر MACD فعال شد.\n"
            "دستور /report برای گزارش وضعیت و "
            "/status برای بررسی اتصال استفاده می‌شود.",
            message.chat.id,
        )

    @bot.message_handler(commands=["stop", "unsubscribe"])
    def telegram_stop(message):
        with state_lock:
            subscribed_users.discard(message.chat.id)
            save_state()

        send_message(
            "ارسال هشدار برای این حساب متوقف شد.",
            message.chat.id,
        )

    @bot.message_handler(commands=["report", "status"])
    def telegram_report(message):
        try:
            send_message(build_report(), message.chat.id)
        except Exception:
            log.exception("Report command failed")


def telegram_polling():
    """
    Robust polling with automatic retry.
    The original version killed the thread on the first exception.
    """
    if not bot:
        log.warning("Telegram bot token is not configured.")
        return

    try:
        me = bot.get_me()
        log.info("Telegram bot connected: @%s (id=%s)", me.username, me.id)
    except Exception as exc:
        log.error("Telegram get_me FAILED (invalid token?): %s", exc)
        return

    try:
        bot.delete_webhook(drop_pending_updates=True)
        log.info("Telegram webhook deleted")
    except Exception as exc:
        log.warning("delete_webhook failed (non-critical): %s", exc)

    retry_delay = 5

    while True:
        try:
            log.info("Starting Telegram infinity_polling...")
            bot.infinity_polling(
                skip_pending=True,
                timeout=20,
                long_polling_timeout=20,
            )
            log.warning("infinity_polling returned unexpectedly; restarting...")

        except Exception as exc:
            text = str(exc)

            if "409" in text or "Conflict" in text:
                log.error(
                    "Telegram 409 CONFLICT (another instance polling "
                    "or webhook active). Retrying in %ss...",
                    retry_delay,
                )
            elif "401" in text or "Unauthorized" in text:
                log.error("Telegram 401: invalid BOT_TOKEN. Stopping.")
                return
            else:
                log.exception(
                    "Telegram polling error; retrying in %ss",
                    retry_delay,
                )

            time.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 60)
            continue

        retry_delay = 5
        time.sleep(2)


# ============================================================
# TRADINGVIEW MESSAGE PROTOCOL
# ============================================================

HEARTBEAT_PATTERN = re.compile(r"~m~\d+~m~~h~\d+")


def tv_send(ws, method, params):
    payload = json.dumps(
        {"m": method, "p": params},
        separators=(",", ":"),
    )
    ws.send(f"~m~{len(payload)}~m~{payload}")


def extract_tv_messages(raw):
    if not raw:
        return []

    messages = []

    chunks = re.split(r"~m~\d+~m~", raw)

    for chunk in chunks:
        chunk = chunk.strip()

        if not chunk:
            continue

        # Skip heartbeat chunks
        if chunk.startswith("~h~"):
            continue

        try:
            messages.append(json.loads(chunk))
        except Exception:
            continue

    return messages


def extract_ohlc_records(obj):
    records = []

    if isinstance(obj, dict):

        values = obj.get("v")

        if isinstance(values, list) and len(values) >= 5:

            try:
                timestamp = float(values[0])

                if timestamp > 100000000:

                    record = {
                        "time": int(timestamp),
                        "open": float(values[1]),
                        "high": float(values[2]),
                        "low": float(values[3]),
                        "close": float(values[4]),
                    }

                    if all(
                        np.isfinite(record[key])
                        for key in ("open", "high", "low", "close")
                    ):
                        if record["high"] >= record["low"]:
                            records.append(record)

            except (TypeError, ValueError, OverflowError):
                pass

        for value in obj.values():
            records.extend(extract_ohlc_records(value))

    elif isinstance(obj, list):

        for item in obj:
            records.extend(extract_ohlc_records(item))

    return records


# ============================================================
# TRADINGVIEW CANDLE FETCHER
# ============================================================

def fetch_tradingview_candles(
    symbol,
    interval,
    n_bars,
    candle_seconds,
):
    ws = None

    bars_by_time = {}

    server_errors = []

    try:

        ws = websocket.create_connection(
            "wss://data.tradingview.com/socket.io/websocket?from=chart%2F",
            header=[
                "Origin: https://www.tradingview.com",
                "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36",
            ],
            timeout=12,
            enable_multithread=True,
        )

        ws.settimeout(1)

        chart_session = "cs_" + uuid.uuid4().hex[:16]

        tv_send(ws, "set_auth_token", ["unauthorized_user_token"])
        tv_send(ws, "chart_create_session", [chart_session, ""])

        symbol_data = json.dumps(
            {"symbol": symbol, "adjustment": "splits"},
            separators=(",", ":"),
        )

        tv_send(
            ws,
            "resolve_symbol",
            [chart_session, "symbol_1", "=" + symbol_data],
        )

        tv_send(
            ws,
            "create_series",
            [
                chart_session,
                "series_1",
                "s1",
                "symbol_1",
                str(interval),
                int(n_bars),
                "",
            ],
        )

        start_time = time.time()
        last_data_time = None
        connection_broken = False

        while time.time() - start_time < TV_TIMEOUT:

            try:
                raw = ws.recv()

            except websocket.WebSocketTimeoutException:
                if bars_by_time and last_data_time is not None:
                    if time.time() - last_data_time >= TV_IDLE_TIMEOUT:
                        break
                continue

            except Exception as exc:
                server_errors.append(str(exc))
                break

            if not raw:
                continue

            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", errors="ignore")

            # ---------------------------------------------------------
            # FIX: Extract heartbeats individually and reply to them.
            # Do NOT skip the entire frame - it may also contain OHLC.
            # ---------------------------------------------------------
            for heartbeat in HEARTBEAT_PATTERN.findall(raw):
                try:
                    ws.send(heartbeat)
                except Exception:
                    connection_broken = True
                    break

            if connection_broken:
                server_errors.append("send heartbeat failed")
                break

            messages = extract_tv_messages(raw)

            for msg in messages:

                method = msg.get("m")
                params = msg.get("p", [])

                if method in (
                    "symbol_error",
                    "series_error",
                    "critical_error",
                ):
                    server_errors.append(str(params))
                    continue

                if method in ("timescale_update", "du"):

                    found = []

                    for param in params:
                        found.extend(extract_ohlc_records(param))

                    if found:
                        for record in found:
                            bars_by_time[record["time"]] = record

                        last_data_time = time.time()

                elif method == "series_completed":
                    if bars_by_time:
                        last_data_time = time.time()

        if ws:
            try:
                ws.close()
            except Exception:
                pass
            ws = None

        if not bars_by_time:

            error = (
                "; ".join(server_errors)
                if server_errors
                else "TradingView returned no OHLC records"
            )

            return None, f"{symbol}: {error}"

        df = pd.DataFrame(list(bars_by_time.values()))

        df = df.sort_values("time")

        df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)

        df = df.drop_duplicates(subset=["time"], keep="last")

        df = df.set_index("time")

        df = df[["open", "high", "low", "close"]].astype(float)

        now = pd.Timestamp.now(tz="UTC")
        closed_until = now - pd.Timedelta(seconds=candle_seconds)

        df = df[df.index <= closed_until]

        df = df[
            np.isfinite(df["open"])
            & np.isfinite(df["high"])
            & np.isfinite(df["low"])
            & np.isfinite(df["close"])
        ]

        df = df[
            (df["high"] >= df["low"])
            & (df["high"] >= df["open"])
            & (df["high"] >= df["close"])
            & (df["low"] <= df["open"])
            & (df["low"] <= df["close"])
        ]

        df = df.sort_index()

        if len(df) > n_bars:
            df = df.iloc[-n_bars:]

        if len(df) < 150:
            return None, (
                f"{symbol}: only {len(df)} closed candles received; "
                f"minimum required is 150"
            )

        return df, None

    except Exception as exc:

        if ws:
            try:
                ws.close()
            except Exception:
                pass

        return None, f"{symbol}: {type(exc).__name__}: {str(exc)[:200]}"


# ============================================================
# SOURCE FAILOVER
# ============================================================

def fetch_market_data(timeframe):
    config = TIMEFRAMES[timeframe]

    previous_source = source_locks.get(timeframe)

    ordered_sources = []

    if previous_source in TV_SOURCES:
        ordered_sources.append(previous_source)

    for source in TV_SOURCES:
        if source not in ordered_sources:
            ordered_sources.append(source)

    errors = []

    for source in ordered_sources:

        try:
            frame, error = fetch_tradingview_candles(
                symbol=source,
                interval=config["interval"],
                n_bars=config["bars"],
                candle_seconds=config["seconds"],
            )
        except Exception as exc:
            frame, error = None, f"{source}: {type(exc).__name__}: {exc}"

        if frame is not None:

            source_changed = (
                previous_source is not None
                and previous_source != source
            )

            source_locks[timeframe] = source

            if source_changed:
                log.warning(
                    "DATA SOURCE CHANGED: %s -> %s (%s)",
                    previous_source,
                    source,
                    timeframe,
                )

            return frame, source, source_changed, None

        errors.append(error or f"{source}: unknown error")

        log.warning(
            "TradingView source failed: %s | %s",
            source,
            error,
        )

    return None, None, False, " | ".join(errors)


# ============================================================
# MACD HISTOGRAM
# ============================================================

def macd_histogram(close, fast, slow, signal):

    fast_ema = close.ewm(span=fast, adjust=False, min_periods=fast).mean()
    slow_ema = close.ewm(span=slow, adjust=False, min_periods=slow).mean()

    macd_line = fast_ema - slow_ema

    signal_line = macd_line.ewm(
        span=signal, adjust=False, min_periods=signal
    ).mean()

    return macd_line - signal_line


# ============================================================
# DIVERGENCE DATA STRUCTURES
# ============================================================

class Extreme:

    def __init__(
        self, index, timestamp, histogram,
        same_bar_price, window_price, phase_extreme_price,
    ):
        self.index = index
        self.time = str(timestamp)
        self.histogram = float(histogram)
        self.same_bar_price = float(same_bar_price)
        self.window_price = float(window_price)
        self.phase_extreme_price = float(phase_extreme_price)


class DivergenceEvent:

    def __init__(
        self, side, first, middle_index,
        middle_histogram, second, message,
    ):
        self.side = side
        self.first = first
        self.middle_index = middle_index
        self.middle_histogram = middle_histogram
        self.second = second
        self.message = message


# ============================================================
# DIVERGENCE TRACKER
# ============================================================

class DivergenceTracker:

    def __init__(self, price_window=4):
        self.price_window = max(0, int(price_window))
        self.reset_all()

    def reset_all(self):
        self.phase = None
        self.first = None
        self.middle_index = None
        self.middle_histogram = None
        self.phase_low = None
        self.phase_high = None
        self.previous_hist = None
        self.previous_previous_hist = None
        self.last_event_key = None
        self.last_processed_time = None

    def price_extreme(self, df, idx, side):

        left = max(0, idx - self.price_window)
        right = min(len(df) - 1, idx + self.price_window)

        segment = df.iloc[left:right + 1]
        row = df.iloc[idx]

        if side == "bull":
            window_price = float(segment["low"].min())
            same_bar = float(row["low"])
            phase_price = (
                self.phase_low if self.phase_low is not None else same_bar
            )
        else:
            window_price = float(segment["high"].max())
            same_bar = float(row["high"])
            phase_price = (
                self.phase_high if self.phase_high is not None else same_bar
            )

        return Extreme(
            idx, df.index[idx], float(df.iloc[idx]["h"]),
            same_bar, window_price, phase_price,
        )

    def handle_bull_trough(self, df, idx):

        h = float(df.iloc[idx]["h"])

        if self.phase != "negative":
            return None

        candidate = self.price_extreme(df, idx, "bull")

        if self.first is None:
            self.first = candidate
            self.middle_index = None
            self.middle_histogram = None
            return None

        if abs(h) > abs(self.first.histogram):
            self.first = candidate
            self.middle_index = None
            self.middle_histogram = None
            return None

        if self.middle_index is None or self.middle_index <= self.first.index:
            return None

        if (
            abs(h) < abs(self.first.histogram)
            and candidate.window_price < self.first.window_price
        ):
            key = ("bull", self.first.index, idx)

            if key == self.last_event_key:
                return None

            self.last_event_key = key

            event = DivergenceEvent(
                "bull", self.first, self.middle_index,
                self.middle_histogram, candidate,
                (
                    f"BULLISH DIVERGENCE | "
                    f"H1={self.first.histogram:.6f}, H2={h:.6f}, "
                    f"Price1={self.first.window_price}, "
                    f"Price2={candidate.window_price}"
                ),
            )

            self.first = candidate
            self.middle_index = None
            self.middle_histogram = None

            return event

        return None

    def handle_bear_peak(self, df, idx):

        h = float(df.iloc[idx]["h"])

        if self.phase != "positive":
            return None

        candidate = self.price_extreme(df, idx, "bear")

        if self.first is None:
            self.first = candidate
            self.middle_index = None
            self.middle_histogram = None
            return None

        if abs(h) > abs(self.first.histogram):
            self.first = candidate
            self.middle_index = None
            self.middle_histogram = None
            return None

        if self.middle_index is None or self.middle_index <= self.first.index:
            return None

        if (
            abs(h) < abs(self.first.histogram)
            and candidate.window_price > self.first.window_price
        ):
            key = ("bear", self.first.index, idx)

            if key == self.last_event_key:
                return None

            self.last_event_key = key

            event = DivergenceEvent(
                "bear", self.first, self.middle_index,
                self.middle_histogram, candidate,
                (
                    f"BEARISH DIVERGENCE | "
                    f"H1={self.first.histogram:.6f}, H2={h:.6f}, "
                    f"Price1={self.first.window_price}, "
                    f"Price2={candidate.window_price}"
                ),
            )

            self.first = candidate
            self.middle_index = None
            self.middle_histogram = None

            return event

        return None

    def update(self, df):

        events = []

        if len(df) < 3:
            return events

        if self.last_processed_time is None:
            start = 0
        elif self.last_processed_time in df.index:
            start = int(df.index.get_loc(self.last_processed_time)) + 1
        else:
            self.reset_all()
            start = 0

        for i in range(start, len(df)):

            h = float(df.iloc[i]["h"])
            low = float(df.iloc[i]["low"])
            high = float(df.iloc[i]["high"])

            self.phase_low = (
                low if self.phase_low is None else min(self.phase_low, low)
            )
            self.phase_high = (
                high if self.phase_high is None else max(self.phase_high, high)
            )

            phase_now = (
                "negative" if h < 0
                else "positive" if h > 0
                else "zero"
            )

            if self.phase is None:
                self.phase = phase_now
            elif phase_now != self.phase and phase_now != "zero":
                self.phase = phase_now
                self.first = None
                self.middle_index = None
                self.middle_histogram = None
                self.phase_low = low
                self.phase_high = high

            if i >= 2:

                h0 = float(df.iloc[i - 2]["h"])
                h1 = float(df.iloc[i - 1]["h"])
                h2 = h

                is_trough = (h1 < 0 and h1 <= h0 and h1 < h2)
                is_peak = (h1 > 0 and h1 >= h0 and h1 > h2)

                if is_trough:
                    event = self.handle_bull_trough(df, i - 1)
                    if event:
                        events.append(event)

                if is_peak:
                    event = self.handle_bear_peak(df, i - 1)
                    if event:
                        events.append(event)

            if (
                self.phase == "negative"
                and self.first is not None
                and i > self.first.index
                and h < 0
            ):
                if self.middle_histogram is None or h > self.middle_histogram:
                    self.middle_histogram = h
                    self.middle_index = i

            if (
                self.phase == "positive"
                and self.first is not None
                and i > self.first.index
                and h > 0
            ):
                if self.middle_histogram is None or h < self.middle_histogram:
                    self.middle_histogram = h
                    self.middle_index = i

            self.previous_previous_hist = self.previous_hist
            self.previous_hist = h

        self.last_processed_time = df.index[-1]

        return events


# ============================================================
# STRATEGY ENGINE
# ============================================================

class StrategyEngine:

    def __init__(self):
        self.trackers = {}
        self.ready_state = {}
        self.fire_armed = {}
        self.last_bar_time = {}
        self.fire_history = {}

    def reset_key(self, key):
        self.trackers.pop(key, None)
        self.ready_state.pop(key, None)
        self.fire_armed.pop(key, None)
        self.last_bar_time.pop(key, None)
        self.fire_history.pop(key, None)

    def process(self, symbol, timeframe, candles):

        key = (symbol, timeframe)

        df = candles.copy().sort_index()

        if len(df) < 150:
            return []

        h1 = macd_histogram(df["close"], 48, 104, 36)
        h2 = macd_histogram(df["close"], 12, 26, 9)
        h3 = macd_histogram(df["close"], 3, 6, 2)

        work = df.copy()
        work["h"] = h1

        tracker = self.trackers.setdefault(
            key, DivergenceTracker(PRICE_MATCH_WINDOW)
        )

        emitted = []

        valid_work = work.dropna(subset=["h"])

        for event in tracker.update(valid_work):
            emitted.append({
                "type": (
                    "DIVERGENCE_BULL"
                    if event.side == "bull"
                    else "DIVERGENCE_BEAR"
                ),
                "time": event.second.time,
                "message": event.message,
            })

        aligned = pd.DataFrame({"h2": h2, "h3": h3}).dropna()

        if len(aligned) < 3:
            return emitted

        last_time = self.last_bar_time.get(key)

        if last_time is None:
            new_rows = aligned.iloc[-2:]
        elif last_time in aligned.index:
            new_rows = aligned.loc[aligned.index > last_time]
        else:
            new_rows = aligned.iloc[-2:]

        history = self.fire_history.setdefault(key, [])

        previous_h2 = None

        for timestamp, row in new_rows.iterrows():

            now2 = float(row["h2"])
            now3 = float(row["h3"])

            if previous_h2 is None:
                pos = aligned.index.get_loc(timestamp)
                if pos > 0:
                    previous_h2 = float(aligned.iloc[pos - 1]["h2"])

            # READY: negative to positive histogram crossing.
            if previous_h2 is not None and previous_h2 <= 0 < now2:
                if not self.ready_state.get(key, False):
                    self.ready_state[key] = True
                    self.fire_armed[key] = False
                    history.clear()
                    emitted.append({
                        "type": "READY",
                        "time": timestamp.isoformat(),
                        "message": (
                            "READY: MACD(12,26,9) "
                            "histogram crossed above zero."
                        ),
                    })

            previous_h2 = now2

            # FIRE: first confirmed negative histogram trough.
            if self.ready_state.get(key, False):

                if (
                    len(history) >= 1
                    and history[-1] >= 0
                    and now3 < 0
                ):
                    self.fire_armed[key] = True
                    history[:] = [now3]

                elif self.fire_armed.get(key, False):

                    if len(history) >= 2:
                        older = history[-2]
                        previous = history[-1]

                        if (
                            previous < 0
                            and previous <= older
                            and previous < now3
                        ):
                            emitted.append({
                                "type": "FIRE",
                                "time": timestamp.isoformat(),
                                "message": (
                                    "FIRE: first confirmed negative "
                                    "MACD(3,6,2) histogram extremum."
                                ),
                            })
                            self.ready_state[key] = False
                            self.fire_armed[key] = False
                            history.clear()

                    history.append(now3)
                    history[:] = history[-2:]

                else:
                    history.append(now3)
                    history[:] = history[-2:]

            self.last_bar_time[key] = timestamp

        return emitted


engine = StrategyEngine()


# ============================================================
# EVENT MANAGEMENT
# ============================================================

def register_event(symbol, timeframe, event):

    event_time = pd.Timestamp(event["time"])

    if event_time.tzinfo is None:
        event_time = event_time.tz_localize("UTC")

    event_time = event_time.tz_convert("UTC")

    event_id = (
        f"{symbol}|{timeframe}|{event['type']}|{event_time.isoformat()}"
    )

    with state_lock:
        if event_id in seen_event_ids:
            return

        seen_event_ids.add(event_id)

        event_history.append({
            "id": event_id,
            "symbol": symbol,
            "timeframe": timeframe,
            "type": event["type"],
            "time": event_time.isoformat(),
            "message": event["message"],
        })

        event_history[:] = event_history[-1000:]

        save_state()

    alert = (
        f"🔔 {symbol} | {timeframe}\n"
        f"{event['type']}\n\n"
        f"{event['message']}\n\n"
        f"زمان تهران: {format_time(event_time)}"
    )

    try:
        broadcast(alert)
    except Exception:
        log.exception("Alert broadcast failed")

    log.info("EVENT: %s", alert)


def recent_events(hours=24):

    cutoff = utc_now() - timedelta(hours=hours)

    with state_lock:
        events = list(event_history)

    result = []

    for item in events:
        try:
            timestamp = pd.Timestamp(item["time"])
            if timestamp.tzinfo is None:
                timestamp = timestamp.tz_localize("UTC")
            if timestamp.to_pydatetime() >= cutoff:
                result.append(item)
        except Exception:
            continue

    return result


# ============================================================
# REPORT
# ============================================================

def build_report():

    try:
        events = recent_events(24)
    except Exception:
        log.exception("recent_events failed inside build_report")
        events = []

    lines = ["MACD DIVERGENCE SCANNER", "Period: Last 24 Hours", ""]

    for timeframe in TIMEFRAMES:

        divergence_count = sum(
            1 for event in events
            if event["timeframe"] == timeframe
            and event["type"] in ("DIVERGENCE_BULL", "DIVERGENCE_BEAR")
        )
        ready_count = sum(
            1 for event in events
            if event["timeframe"] == timeframe
            and event["type"] == "READY"
        )
        fire_count = sum(
            1 for event in events
            if event["timeframe"] == timeframe
            and event["type"] == "FIRE"
        )

        lines.extend([
            f"Symbol Report: {SYMBOL}",
            f"Timeframe: {timeframe}",
            f"Source: {source_locks.get(timeframe, 'Not connected')}",
            f"Candles: {latest_data.get(timeframe, {}).get('count', 0)}",
            f"DIVERGENCE: {divergence_count}",
            f"READY: {ready_count}",
            f"FIRE: {fire_count}",
            "",
        ])

        error = latest_errors.get(timeframe)
        if error:
            lines.append(f"DATA ERROR: {error}")
            lines.append("")

    lines.append("Recent Events:")

    if not events:
        lines.append("No recorded events in the last 24 hours.")
    else:
        for event in sorted(
            events, key=lambda item: item["time"], reverse=True
        )[:30]:
            lines.append(
                f"{event['timeframe']} | {event['type']} | "
                f"{format_time(event['time'])}"
            )
            lines.append(event["message"])
            lines.append("")

    return "\n".join(lines)


# ============================================================
# SCANNER
# ============================================================

def scan_timeframe(timeframe):

    log.info("Fetching %s candles", timeframe)

    frame, source, source_changed, error = fetch_market_data(timeframe)

    if frame is None:
        latest_errors[timeframe] = error
        log.error("No candles returned for %s: %s", timeframe, error)
        return

    latest_errors[timeframe] = None

    latest_data[timeframe] = {
        "count": len(frame),
        "last_time": frame.index[-1].isoformat(),
        "last_close": float(frame["close"].iloc[-1]),
    }

    if source_changed:
        engine.reset_key((SYMBOL, timeframe))
        log.warning(
            "Strategy state reset because data source changed: %s",
            timeframe,
        )

    log.info(
        "DATA OK | %s | %s | candles=%s | last_close=%s",
        timeframe, source, len(frame),
        format_price(frame["close"].iloc[-1]),
    )

    events = engine.process(SYMBOL, timeframe, frame)

    for event in events:
        try:
            register_event(SYMBOL, timeframe, event)
        except Exception:
            log.exception("register_event failed")


def scanner_loop():
    """
    The outer while-loop is bulletproof:
    - NEVER dies from an exception
    - ALWAYS sleeps at least 1s between cycles
    - ALWAYS reports accurate state via scanner_started
    """
    global scanner_started

    log.info("MACD scanner started")
    scanner_started = True

    try:
        while True:

            cycle_start = time.time()

            # 1) Scan each timeframe (each protected individually)
            for timeframe in TIMEFRAMES:
                try:
                    scan_timeframe(timeframe)
                except Exception:
                    log.exception(
                        "Scanner error on timeframe %s", timeframe
                    )

            # 2) Build & log report (protected so failure doesn't kill us)
            try:
                log.info("\n%s", build_report())
            except Exception:
                log.exception("build_report failed inside scanner_loop")

            # 3) Sleep (always ≥1s)
            try:
                elapsed = time.time() - cycle_start
                sleep_time = max(1, SCAN_INTERVAL - elapsed)
            except Exception:
                sleep_time = SCAN_INTERVAL

            time.sleep(sleep_time)

    except BaseException:
        scanner_started = False
        log.exception("scanner_loop terminated unexpectedly")
        raise


# ============================================================
# FLASK ENDPOINTS
# ============================================================

@app.get("/")
def index():
    return jsonify({
        "service": "MACD Divergence Scanner",
        "status": "running",
        "timeframes": list(TIMEFRAMES.keys()),
    })


@app.get("/health")
def health():
    return jsonify({
        "status": "ok",
        "scanner_started": scanner_started,
        "sources": source_locks,
        "latest_data": latest_data,
        "errors": latest_errors,
    })


@app.get("/report")
def report():
    try:
        return app.response_class(build_report(), mimetype="text/plain")
    except Exception:
        log.exception("report endpoint failed")
        return app.response_class(
            "Report generation failed; check logs.",
            mimetype="text/plain",
            status=500,
        )


@app.get("/events")
def events_endpoint():
    return jsonify(recent_events(24))


# ============================================================
# STARTUP
# ============================================================

def start_background_threads():

    scanner_thread = threading.Thread(
        target=scanner_loop,
        daemon=True,
        name="market-scanner",
    )
    scanner_thread.start()

    if bot:
        telegram_thread = threading.Thread(
            target=telegram_polling,
            daemon=True,
            name="telegram-polling",
        )
        telegram_thread.start()


if __name__ == "__main__":

    log.info("Starting MACD Divergence Scanner")

    start_background_threads()

    app.run(
        host=HOST,
        port=PORT,
        debug=False,
        use_reloader=False,
        threaded=True,
    )
