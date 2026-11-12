from __future__ import annotations

import os
import re
import json
import time
import logging
import random
import string
import threading
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
import pytz
import websocket
import telebot

from flask import Flask, jsonify

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


# ============================================================
# CONFIGURATION
# ============================================================

HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8080"))

TEHRAN = pytz.timezone("Asia/Tehran")

TIMEFRAME_CONFIG = {
    "1": 1440,
    "5": 150,
}

SCAN_INTERVALS = {
    "1": 60,
    "5": 300,
}

PRICE_MATCH_WINDOW = int(
    os.getenv("PRICE_MATCH_WINDOW", "4")
)

SYMBOLS = {
    "XAUUSD": [
        "OANDA:XAUUSD",
        "FOREXCOM:XAUUSD",
        "FX_IDC:XAUUSD",
    ],
    "EURUSD": [
        "OANDA:EURUSD",
        "FOREXCOM:EURUSD",
        "FX:EURUSD",
    ],
    "GBPUSD": [
        "OANDA:GBPUSD",
        "FOREXCOM:GBPUSD",
        "FX:GBPUSD",
    ],
    "USDJPY": [
        "OANDA:USDJPY",
        "FOREXCOM:USDJPY",
        "FX:USDJPY",
    ],
    "USDCHF": [
        "OANDA:USDCHF",
        "FOREXCOM:USDCHF",
        "FX:USDCHF",
    ],
    "USDCAD": [
        "OANDA:USDCAD",
        "FOREXCOM:USDCAD",
        "FX:USDCAD",
    ],
    "AUDUSD": [
        "OANDA:AUDUSD",
        "FOREXCOM:AUDUSD",
        "FX:AUDUSD",
    ],
    "NZDUSD": [
        "OANDA:NZDUSD",
        "FOREXCOM:NZDUSD",
        "FX:NZDUSD",
    ],
}

MACD_STRUCTURE = (48, 104, 36)
MACD_READY = (12, 26, 9)
MACD_FIRE = (3, 6, 2)


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

log = logging.getLogger("MACD_ENGINE")


# ============================================================
# TELEGRAM
# ============================================================

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

bot = (
    telebot.TeleBot(BOT_TOKEN)
    if BOT_TOKEN and CHAT_ID
    else None
)


def send_alert(message: str):
    if bot is None:
        log.info("ALERT:\n%s", message)
        return

    try:
        bot.send_message(
            CHAT_ID,
            message,
            disable_web_page_preview=True,
        )
    except Exception:
        log.exception("Telegram delivery failed")


# ============================================================
# PRICE FORMATTING
# ============================================================

def format_price(symbol, price):
    digits = 2 if symbol.endswith("JPY") else 4
    return f"{float(price):.{digits}f}"


def to_tehran(timestamp):
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize("UTC")

    return timestamp.tz_convert(TEHRAN).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


# ============================================================
# TRADINGVIEW WEBSOCKET
# ============================================================

TV_URL = (
    "wss://data.tradingview.com/"
    "socket.io/websocket?from=chart%2F"
)


def tv_send(ws, method, params):
    payload = json.dumps(
        {"m": method, "p": params},
        separators=(",", ":"),
    )

    ws.send(
        f"~m~{len(payload)}~m~{payload}"
    )


def decode_tv_frames(raw):
    messages = []
    heartbeats = []

    if not raw:
        return messages, heartbeats

    if raw.startswith("~h~"):
        heartbeats.append(raw)
        return messages, heartbeats

    position = 0

    while True:
        start = raw.find("~m~", position)

        if start < 0:
            break

        length_start = start + 3
        separator = raw.find("~m~", length_start)

        if separator < 0:
            break

        length_text = raw[length_start:separator]

        if not length_text.isdigit():
            position = length_start
            continue

        payload_start = separator + 3
        payload_length = int(length_text)
        payload_end = payload_start + payload_length

        if payload_end > len(raw):
            break

        payload = raw[payload_start:payload_end]

        if payload.startswith("~h~"):
            heartbeats.append(
                raw[start:payload_end]
            )
        else:
            try:
                messages.append(json.loads(payload))
            except Exception:
                pass

        position = payload_end

    return messages, heartbeats


def fetch_candles(
    tv_symbol,
    interval="1",
    n_bars=1440,
    timeout=20,
):
    ws = None

    try:
        ws = websocket.create_connection(
            TV_URL,
            header=[
                "Origin: https://www.tradingview.com",
                "User-Agent: Mozilla/5.0",
            ],
            timeout=12,
            enable_multithread=True,
        )

        chart_session = (
            "cs_"
            + "".join(
                random.choices(
                    string.ascii_lowercase
                    + string.digits,
                    k=12,
                )
            )
        )

        tv_send(
            ws,
            "set_auth_token",
            ["unauthorized_user_token"],
        )

        tv_send(
            ws,
            "chart_create_session",
            [chart_session, ""],
        )

        symbol_json = json.dumps(
            {
                "symbol": tv_symbol,
                "adjustment": "splits",
            },
            separators=(",", ":"),
        )

        tv_send(
            ws,
            "resolve_symbol",
            [
                chart_session,
                "sds_sym_1",
                f"={symbol_json}",
            ],
        )

        tv_send(
            ws,
            "create_series",
            [
                chart_session,
                "sds_1",
                "s1",
                "sds_sym_1",
                str(interval),
                int(n_bars) + 1,
                "",
            ],
        )

        bars = {}
        completed = False
        started = time.monotonic()

        while time.monotonic() - started < timeout:

            try:
                raw = ws.recv()
            except websocket.WebSocketTimeoutException:
                continue

            if not raw:
                break

            messages, heartbeats = decode_tv_frames(raw)

            for heartbeat in heartbeats:
                ws.send(heartbeat)

            for msg in messages:

                method = msg.get("m")
                params = msg.get("p", [])

                if method in ("timescale_update", "du"):

                    if len(params) < 2:
                        continue

                    if not isinstance(params[1], dict):
                        continue

                    series = params[1].get(
                        "sds_1",
                        {},
                    )

                    for item in series.get("s", []):

                        values = item.get("v", [])

                        if len(values) < 5:
                            continue

                        try:
                            timestamp = int(values[0])

                            open_price = float(values[1])
                            high_price = float(values[2])
                            low_price = float(values[3])
                            close_price = float(values[4])

                            if not all(
                                pd.notna(v)
                                for v in (
                                    open_price,
                                    high_price,
                                    low_price,
                                    close_price,
                                )
                            ):
                                continue

                            if (
                                high_price < low_price
                                or high_price < max(
                                    open_price,
                                    close_price,
                                )
                                or low_price > min(
                                    open_price,
                                    close_price,
                                )
                            ):
                                continue

                            bars[timestamp] = (
                                open_price,
                                high_price,
                                low_price,
                                close_price,
                            )

                        except (ValueError, TypeError):
                            continue

                elif method == "series_completed":
                    completed = True

                elif method in (
                    "symbol_error",
                    "series_error",
                ):
                    raise RuntimeError(
                        str(params)[:200]
                    )

            if completed:
                break

        if not bars:
            raise RuntimeError("No market data received")

        if not completed:
            raise RuntimeError(
                "TradingView series did not complete"
            )

        df = pd.DataFrame.from_dict(
            bars,
            orient="index",
            columns=[
                "open",
                "high",
                "low",
                "close",
            ],
        )

        df = df.sort_index()

        df.index = pd.to_datetime(
            df.index,
            unit="s",
            utc=True,
        )

        df.index.name = "time"

        # Remove only genuinely unfinished bars.
        interval_seconds = int(interval) * 60

        now_utc = pd.Timestamp.now(tz="UTC")

        closed_mask = (
            df.index
            + pd.Timedelta(seconds=interval_seconds)
            <= now_utc
        )

        df = df.loc[closed_mask]

        df = df.tail(int(n_bars))

        if df.empty:
            raise RuntimeError("No closed candles available")

        return df, None

    except Exception as exc:

        return None, (
            f"{tv_symbol}: {str(exc)[:120]}"
        )

    finally:

        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass


# ============================================================
# SYMBOL SOURCE MANAGER
# ============================================================

def get_closed_candles(
    name,
    interval,
    n_bars,
):
    errors = []

    for source in SYMBOLS[name]:

        df, error = fetch_candles(
            source,
            interval=interval,
            n_bars=n_bars,
        )

        if df is not None and len(df) >= 150:

            return df, source, None

        errors.append(
            error or f"{source}: insufficient data"
        )

    return None, None, " | ".join(errors)


# ============================================================
# MACD HISTOGRAM
# ============================================================

def macd_histogram(
    close,
    fast,
    slow,
    signal,
):

    fast_ema = close.ewm(
        span=fast,
        adjust=False,
        min_periods=fast,
    ).mean()

    slow_ema = close.ewm(
        span=slow,
        adjust=False,
        min_periods=slow,
    ).mean()

    macd_line = fast_ema - slow_ema

    signal_line = macd_line.ewm(
        span=signal,
        adjust=False,
        min_periods=signal,
    ).mean()

    return macd_line - signal_line


# ============================================================
# DIVERGENCE DATA STRUCTURES
# ============================================================

class Extreme:

    def __init__(
        self,
        index,
        time,
        histogram,
        same_bar_price,
        window_price,
        phase_extreme_price,
    ):

        self.index = index
        self.time = time
        self.histogram = histogram
        self.same_bar_price = same_bar_price
        self.window_price = window_price
        self.phase_extreme_price = phase_extreme_price


class DivergenceEvent:

    def __init__(
        self,
        side,
        first,
        middle_index,
        middle_histogram,
        second,
        message,
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

        self.price_window = max(
            0,
            int(price_window),
        )

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
        self._last_processed_time = None

    def _price_extreme(
        self,
        df,
        idx,
        side,
    ):

        left = max(
            0,
            idx - self.price_window,
        )

        right = min(
            len(df) - 1,
            idx + self.price_window,
        )

        segment = df.iloc[
            left:right + 1
        ]

        row = df.iloc[idx]

        if side == "bull":

            window_price = float(
                segment["low"].min()
            )

            same_bar = float(row["low"])

            phase_price = (
                self.phase_low
                if self.phase_low is not None
                else same_bar
            )

        else:

            window_price = float(
                segment["high"].max()
            )

            same_bar = float(row["high"])

            phase_price = (
                self.phase_high
                if self.phase_high is not None
                else same_bar
            )

        return Extreme(
            index=idx,
            time=str(df.index[idx]),
            histogram=float(df.iloc[idx]["h"]),
            same_bar_price=same_bar,
            window_price=window_price,
            phase_extreme_price=float(phase_price),
        )

    def update(self, df):

        events = []

        if len(df) < 3:
            return events

        last_time = self._last_processed_time

        if last_time is None:

            start = 0

        elif last_time in df.index:

            start = (
                int(df.index.get_loc(last_time)) + 1
            )

        else:

            self.reset_all()
            start = 0

        for i in range(start, len(df)):

            h = float(df.iloc[i]["h"])

            low = float(df.iloc[i]["low"])
            high = float(df.iloc[i]["high"])

            self.phase_low = (
                low
                if self.phase_low is None
                else min(self.phase_low, low)
            )

            self.phase_high = (
                high
                if self.phase_high is None
                else max(self.phase_high, high)
            )

            phase_now = (
                "negative" if h < 0
                else "positive" if h > 0
                else "zero"
            )

            if self.phase is None:

                self.phase = phase_now

            elif (
                phase_now != self.phase
                and phase_now != "zero"
            ):

                self.phase = phase_now

                self.first = None
                self.middle_index = None
                self.middle_histogram = None

                self.phase_low = low
                self.phase_high = high

            # Local extrema are confirmed by the next closed bar.
            if i >= 2:

                h0 = float(df.iloc[i - 2]["h"])
                h1 = float(df.iloc[i - 1]["h"])
                h2 = h

                is_trough = (
                    h1 < 0
                    and h1 <= h0
                    and h1 < h2
                )

                is_peak = (
                    h1 > 0
                    and h1 >= h0
                    and h1 > h2
                )

                if is_trough:

                    event = self._handle_bull_trough(
                        df,
                        i - 1,
                    )

                    if event:
                        events.append(event)

                if is_peak:

                    event = self._handle_bear_peak(
                        df,
                        i - 1,
                    )

                    if event:
                        events.append(event)

            # Dynamic middle point in negative phase.
            if (
                self.phase == "negative"
                and self.first is not None
            ):

                if (
                    i > self.first.index
                    and h < 0
                ):

                    if (
                        self.middle_histogram is None
                        or h > self.middle_histogram
                    ):

                        self.middle_histogram = h
                        self.middle_index = i

            # Dynamic middle point in positive phase.
            if (
                self.phase == "positive"
                and self.first is not None
            ):

                if (
                    i > self.first.index
                    and h > 0
                ):

                    if (
                        self.middle_histogram is None
                        or h < self.middle_histogram
                    ):

                        self.middle_histogram = h
                        self.middle_index = i

            self.previous_previous_hist = (
                self.previous_hist
            )

            self.previous_hist = h

        if len(df):
            self._last_processed_time = df.index[-1]

        return events

    def _handle_bull_trough(
        self,
        df,
        idx,
    ):

        h = float(df.iloc[idx]["h"])

        if self.phase != "negative":
            return None

        candidate = self._price_extreme(
            df,
            idx,
            "bull",
        )

        if self.first is None:

            self.first = candidate

            self.middle_index = None
            self.middle_histogram = None

            return None

        # A deeper negative trough invalidates
        # the previous reference and middle point.
        if abs(h) > abs(self.first.histogram):

            self.first = candidate

            self.middle_index = None
            self.middle_histogram = None

            return None

        if (
            self.middle_index is None
            or self.middle_index <= self.first.index
        ):

            return None

        # Regular bullish divergence.
        if (
            abs(h) < abs(self.first.histogram)
            and candidate.window_price
            < self.first.window_price
        ):

            key = (
                "bull",
                self.first.time,
                candidate.time,
            )

            if key == self.last_event_key:
                return None

            self.last_event_key = key

            event = DivergenceEvent(
                side="bull",
                first=self.first,
                middle_index=self.middle_index,
                middle_histogram=float(
                    self.middle_histogram
                ),
                second=candidate,
                message=(
                    "BULLISH DIVERGENCE\n"
                    f"H1={self.first.histogram:.6f}\n"
                    f"H2={h:.6f}\n"
                    f"Price1={self.first.window_price}\n"
                    f"Price2={candidate.window_price}"
                ),
            )

            self.first = candidate

            self.middle_index = None
            self.middle_histogram = None

            return event

        return None

    def _handle_bear_peak(
        self,
        df,
        idx,
    ):

        h = float(df.iloc[idx]["h"])

        if self.phase != "positive":
            return None

        candidate = self._price_extreme(
            df,
            idx,
            "bear",
        )

        if self.first is None:

            self.first = candidate

            self.middle_index = None
            self.middle_histogram = None

            return None

        # A stronger positive peak invalidates
        # the previous reference and middle point.
        if abs(h) > abs(self.first.histogram):

            self.first = candidate

            self.middle_index = None
            self.middle_histogram = None

            return None

        if (
            self.middle_index is None
            or self.middle_index <= self.first.index
        ):

            return None

        # Regular bearish divergence.
        if (
            abs(h) < abs(self.first.histogram)
            and candidate.window_price
            > self.first.window_price
        ):

            key = (
                "bear",
                self.first.time,
                candidate.time,
            )

            if key == self.last_event_key:
                return None

            self.last_event_key = key

            event = DivergenceEvent(
                side="bear",
                first=self.first,
                middle_index=self.middle_index,
                middle_histogram=float(
                    self.middle_histogram
                ),
                second=candidate,
                message=(
                    "BEARISH DIVERGENCE\n"
                    f"H1={self.first.histogram:.6f}\n"
                    f"H2={h:.6f}\n"
                    f"Price1={self.first.window_price}\n"
                    f"Price2={candidate.window_price}"
                ),
            )

            self.first = candidate

            self.middle_index = None
            self.middle_histogram = None

            return event

        return None


# ============================================================
# STRATEGY ENGINE
# ============================================================

class StrategyEngine:

    def __init__(self):

        self.divergence_trackers = {}

        self.ready_state = {}
        self.fire_armed = {}

        self.last_bar_time = {}
        self.fire_history = {}

        self.last_divergence_time = {}

        self.lock = threading.RLock()

    def on_candles(
        self,
        symbol,
        timeframe,
        candles,
    ):

        with self.lock:

            df = candles.copy()

            if "time" in df.columns:
                df = df.set_index("time")

            required = {
                "open",
                "high",
                "low",
                "close",
            }

            missing = required.difference(df.columns)

            if missing:
                raise ValueError(
                    f"Missing OHLC columns: {sorted(missing)}"
                )

            if "closed" in df.columns:
                df = df[df["closed"].astype(bool)]

            df = df.sort_index()

            df = df[
                ["open", "high", "low", "close"]
            ].astype(float)

            if len(df) < 150:
                return []

            key = (symbol, timeframe)

            h1 = macd_histogram(
                df["close"],
                *MACD_STRUCTURE,
            )

            h2 = macd_histogram(
                df["close"],
                *MACD_READY,
            )

            h3 = macd_histogram(
                df["close"],
                *MACD_FIRE,
            )

            work = df.copy()
            work["h"] = h1

            tracker = self.divergence_trackers.setdefault(
                key,
                DivergenceTracker(
                    price_window=PRICE_MATCH_WINDOW
                ),
            )

            emitted = []

            for event in tracker.update(
                work.dropna(subset=["h"])
            ):

                self.last_divergence_time[key] = (
                    event.second.time
                )

                message = (
                    f"{symbol} {timeframe}\n"
                    f"{event.message}\n"
                    f"Time: {to_tehran(event.second_time) if hasattr(event, 'second_time') else to_tehran(pd.Timestamp(event.second.time))}"
                )

                send_alert(message)
                emitted.append(message)

            aligned = pd.DataFrame(
                {
                    "h2": h2,
                    "h3": h3,
                }
            ).dropna()

            if len(aligned) < 3:
                return emitted

            last_time = self.last_bar_time.get(key)

            if last_time is None:

                new_rows = aligned.iloc[-2:]

            elif last_time in aligned.index:

                new_rows = aligned.loc[
                    aligned.index > last_time
                ]

            else:

                new_rows = aligned.iloc[-2:]

            history = self.fire_history.setdefault(
                key,
                [],
            )

            previous_h2 = None

            for timestamp, row in new_rows.iterrows():

                now2 = float(row["h2"])
                now3 = float(row["h3"])

                if previous_h2 is None:

                    pos = aligned.index.get_loc(timestamp)

                    if pos > 0:

                        previous_h2 = float(
                            aligned.iloc[pos - 1]["h2"]
                        )

                # READY:
                # MACD(12,26,9) crosses from negative to positive.
                if (
                    previous_h2 is not None
                    and previous_h2 <= 0 < now2
                ):

                    if not self.ready_state.get(key, False):

                        self.ready_state[key] = True
                        self.fire_armed[key] = False

                        history.clear()

                        message = (
                            f"{symbol} {timeframe}\n"
                            "READY\n"
                            "MACD(12,26,9) histogram "
                            "crossed above zero."
                        )

                        send_alert(message)
                        emitted.append(message)

                previous_h2 = now2

                # FIRE:
                # After READY, MACD(3,6,2) enters
                # negative phase and forms its first trough.
                if self.ready_state.get(key, False):

                    if (
                        len(history) >= 1
                        and history[-1] >= 0 > now3
                    ):

                        self.fire_armed[key] = True

                        history[:] = [now3]

                    elif self.fire_armed.get(key, False):

                        if len(history) >= 2:

                            older, previous = (
                                history[-2],
                                history[-1],
                            )

                            if (
                                previous < 0
                                and previous <= older
                                and previous < now3
                            ):

                                message = (
                                    f"{symbol} {timeframe}\n"
                                    "FIRE\n"
                                    "First confirmed negative "
                                    "MACD(3,6,2) histogram extremum."
                                )

                                send_alert(message)
                                emitted.append(message)

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


# ============================================================
# GLOBAL ENGINE
# ============================================================

engine = StrategyEngine()


# ============================================================
# MARKET SCANNER
# ============================================================

def scan_symbol(name, interval):

    n_bars = TIMEFRAME_CONFIG[interval]

    df, source, error = get_closed_candles(
        name,
        interval,
        n_bars,
    )

    if df is None:

        log.warning(
            "%s M%s failed: %s",
            name,
            interval,
            error,
        )

        return None

    log.info(
        "%s M%s source=%s bars=%d last=%s",
        name,
        interval,
        source,
        len(df),
        to_tehran(df.index[-1]),
    )

    return name, df, source


def scan_timeframe(interval):

    log.info(
        "Starting M%s scan",
        interval,
    )

    results = []

    with ThreadPoolExecutor(
        max_workers=4
    ) as executor:

        futures = {
            executor.submit(
                scan_symbol,
                name,
                interval,
            ): name
            for name in SYMBOLS
        }

        for future in as_completed(futures):

            try:

                result = future.result()

                if result is not None:
                    results.append(result)

            except Exception:

                log.exception(
                    "Symbol scanning error"
                )

    for name, df, source in results:

        try:

            events = engine.on_candles(
                symbol=name,
                timeframe=f"M{interval}",
                candles=df,
            )

            if events:

                log.info(
                    "%s M%s emitted %d alerts",
                    name,
                    interval,
                    len(events),
                )

        except Exception:

            log.exception(
                "Strategy processing failed: %s M%s",
                name,
                interval,
            )

    log.info(
        "Completed M%s scan: %d/%d symbols",
        interval,
        len(results),
        len(SYMBOLS),
    )


# ============================================================
# BACKGROUND SCANNER
# ============================================================

def scanner_loop():

    next_scan = {
        "1": 0,
        "5": 0,
    }

    while True:

        try:

            now = time.monotonic()

            for interval in ("1", "5"):

                if now >= next_scan[interval]:

                    scan_timeframe(interval)

                    next_scan[interval] = (
                        time.monotonic()
                        + SCAN_INTERVALS[interval]
                    )

            time.sleep(1)

        except Exception:

            log.exception(
                "Scanner loop failure"
            )

            time.sleep(10)


# ============================================================
# FLASK SERVER
# ============================================================

app = Flask(__name__)


@app.get("/")
def index():

    return jsonify(
        message="MACD Divergence Engine is running",
        service="TradingView WebSocket Scanner",
    )


@app.get("/health")
def health():

    return jsonify(
        status="ok",
        service="macd-divergence-engine",
        strategy="histogram-only",
        macd={
            "structure": MACD_STRUCTURE,
            "ready": MACD_READY,
            "fire": MACD_FIRE,
        },
        timeframes={
            "M1": 1440,
            "M5": 150,
        },
        symbols=list(SYMBOLS.keys()),
        telegram=bot is not None,
    )


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":

    log.info(
        "Starting MACD Divergence Engine"
    )

    log.info(
        "Symbols: %s",
        ", ".join(SYMBOLS.keys()),
    )

    log.info(
        "M1 history: 1440 candles"
    )

    log.info(
        "M5 history: 150 candles"
    )

    scanner_thread = threading.Thread(
        target=scanner_loop,
        daemon=True,
        name="MarketScanner",
    )

    scanner_thread.start()

    app.run(
        host=HOST,
        port=PORT,
        debug=False,
        use_reloader=False,
        threaded=True,
    )
