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
import requests
from flask import Flask, jsonify

# ---- Optional providers (import safely) ----
try:
    import yfinance as yf
    HAS_YFINANCE = True
except ImportError:
    HAS_YFINANCE = False
    yf = None


# ============================================================
# CONFIGURATION
# ============================================================

PORT = int(os.getenv("PORT", "8080"))
HOST = os.getenv("HOST", "0.0.0.0")

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", os.getenv("BOT_TOKEN", "")).strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", "30"))
TV_TIMEOUT = int(os.getenv("TV_TIMEOUT", "20"))
TV_IDLE_TIMEOUT = float(os.getenv("TV_IDLE_TIMEOUT", "4"))
HTTP_TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "12"))

STATE_FILE = os.getenv("STATE_FILE", "scanner_state.json")
PRICE_MATCH_WINDOW = 7  # ۷ کندل رو به عقب برای قیمت کف/سقف

TEHRAN = pytz.timezone("Asia/Tehran")

# ۸ نماد (طلا و هفت ماژور)
SYMBOLS = [
    "XAUUSD",
    "EURUSD",
    "GBPUSD",
    "USDJPY",
    "AUDUSD",
    "USDCAD",
    "NZDUSD",
    "USDCHF",
]

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

TV_SOURCES_MAP = {
    "XAUUSD": ["OANDA:XAUUSD", "FOREXCOM:XAUUSD", "FX_IDC:XAUUSD", "BINANCE:PAXGUSDT"],
    "EURUSD": ["OANDA:EURUSD", "FOREXCOM:EURUSD", "FX_IDC:EURUSD"],
    "GBPUSD": ["OANDA:GBPUSD", "FOREXCOM:GBPUSD", "FX_IDC:GBPUSD"],
    "USDJPY": ["OANDA:USDJPY", "FOREXCOM:USDJPY", "FX_IDC:USDJPY"],
    "AUDUSD": ["OANDA:AUDUSD", "FOREXCOM:AUDUSD", "FX_IDC:AUDUSD"],
    "USDCAD": ["OANDA:USDCAD", "FOREXCOM:USDCAD", "FX_IDC:USDCAD"],
    "NZDUSD": ["OANDA:NZDUSD", "FOREXCOM:NZDUSD", "FX_IDC:NZDUSD"],
    "USDCHF": ["OANDA:USDCHF", "FOREXCOM:USDCHF", "FX_IDC:USDCHF"],
}

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(message)s",
)

log = logging.getLogger("robust-macd-scanner")

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
    global subscribed_users, event_history, seen_event_ids
    try:
        if not os.path.exists(STATE_FILE):
            return
        with open(STATE_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)
        subscribed_users = set(data.get("users", []))
        event_history = data.get("events", [])
        seen_event_ids = {
            item.get("id") for item in event_history if item.get("id")
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
                json.dump(payload, file, ensure_ascii=False, indent=2)
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


def format_price(value, symbol="XAUUSD"):
    digits = 2 if symbol.endswith("JPY") else 4
    return f"{float(value):.{digits}f}"


# ============================================================
# TELEGRAM BOT HANDLERS
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
            bot.send_message(target, message, disable_web_page_preview=True, parse_mode="HTML")
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
            "عضویت شما در اسکنر تخصصی MACD فعال شد.\n"
            "دستور /report برای گزارش وضعیت یا ارسال نام نماد (مثلا XAUUSD) برای مشاهده آخرین FIRE.",
            message.chat.id,
        )

    @bot.message_handler(commands=["stop", "unsubscribe"])
    def telegram_stop(message):
        with state_lock:
            subscribed_users.discard(message.chat.id)
            save_state()
        send_message("ارسال هشدار برای این حساب متوقف شد.", message.chat.id)

    @bot.message_handler(commands=["report", "status"])
    def telegram_report(message):
        send_message(build_report(), message.chat.id)

    @bot.message_handler(func=lambda m: m.text and m.text.upper() in SYMBOLS)
    def telegram_symbol_history(message):
        sym = message.text.upper()
        last_fire = engine.get_last_fire(sym)
        if last_fire:
            send_message(
                f"📊 <b>تاریخچه آخرین FIRE برای نماد {sym}</b>\n\n"
                f"📌 نوع: {last_fire['type']} ({last_fire.get('side', '')})\n"
                f"🕒 زمان (به‌وقت تهران): <b>{format_time(last_fire['time'])}</b>\n"
                f"💬 پیام: {last_fire['message']}",
                message.chat.id
            )
        else:
            send_message(f"در حافظه فعلی هیچ رویداد FIRE برای نماد {sym} ثبت نشده است.", message.chat.id)


def telegram_polling():
    if not bot:
        log.warning("Telegram bot token is not configured.")
        return
    try:
        bot.remove_webhook()
        time.sleep(1)
    except Exception:
        pass

    while True:
        try:
            bot.infinity_polling(skip_pending=True, timeout=20, long_polling_timeout=20)
        except Exception:
            log.exception("Telegram polling stopped; retrying in 5s")
            time.sleep(5)


# ============================================================
# DATA PROVIDERS (TradingView WebSocket - Exact Robust Pattern)
# ============================================================

HEARTBEAT_PATTERN = re.compile(r"~m~\d+~m~~h~\d+")

def tv_send(ws, method, params):
    payload = json.dumps({"m": method, "p": params}, separators=(",", ":"))
    ws.send(f"~m~{len(payload)}~m~{payload}")


def extract_tv_messages(raw):
    if not raw:
        return []
    messages = []
    for chunk in re.split(r"~m~\d+~m~", raw):
        chunk = chunk.strip()
        if not chunk or chunk.startswith("~h~"):
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
                    if all(np.isfinite(record[k]) for k in ("open", "high", "low", "close")) and record["high"] >= record["low"]:
                        records.append(record)
            except (TypeError, ValueError, OverflowError):
                pass
        for value in obj.values():
            records.extend(extract_ohlc_records(value))
    elif isinstance(obj, list):
        for item in obj:
            records.extend(extract_ohlc_records(item))
    return records


def fetch_from_tradingview(symbol, interval, n_bars, candle_seconds):
    ws = None
    bars_by_time = {}
    server_errors = []
    try:
        ws = websocket.create_connection(
            "wss://data.tradingview.com/socket.io/websocket?from=chart%2F",
            header=[
                "Origin: https://www.tradingview.com",
                "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            ],
            timeout=10,
            enable_multithread=True,
        )
        ws.settimeout(1)
        chart_session = "cs_" + uuid.uuid4().hex[:16]

        tv_send(ws, "set_auth_token", ["unauthorized_user_token"])
        tv_send(ws, "chart_create_session", [chart_session, ""])

        symbol_data = json.dumps({"symbol": symbol, "adjustment": "splits"}, separators=(",", ":"))
        tv_send(ws, "resolve_symbol", [chart_session, "symbol_1", "=" + symbol_data])
        tv_send(ws, "create_series", [chart_session, "series_1", "s1", "symbol_1", str(interval), int(n_bars), ""])

        start_time = time.time()
        last_data_time = None

        while time.time() - start_time < TV_TIMEOUT:
            try:
                raw = ws.recv()
            except websocket.WebSocketTimeoutException:
                if bars_by_time and last_data_time and time.time() - last_data_time >= TV_IDLE_TIMEOUT:
                    break
                continue
            except Exception as exc:
                server_errors.append(str(exc))
                break

            if not raw:
                continue
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", errors="ignore")

            for hb in HEARTBEAT_PATTERN.findall(raw):
                try:
                    ws.send(hb)
                except Exception:
                    pass

            for msg in extract_tv_messages(raw):
                method = msg.get("m")
                params = msg.get("p", [])
                if method in ("symbol_error", "series_error", "critical_error"):
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
            ws.close()

        if not bars_by_time:
            return None, f"TV {symbol}: {'; '.join(server_errors) if server_errors else 'no records'}"

        df = pd.DataFrame(list(bars_by_time.values())).sort_values("time")
        df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
        df = df.drop_duplicates(subset=["time"], keep="last").set_index("time")
        df = df[["open", "high", "low", "close"]].astype(float)

        now = pd.Timestamp.now(tz="UTC")
        df = df[df.index <= now - pd.Timedelta(seconds=candle_seconds)]
        df = df[np.isfinite(df).all(axis=1)].sort_index()

        if len(df) > n_bars:
            df = df.iloc[-n_bars:]
        if len(df) < 50:
            return None, f"TV {symbol}: only {len(df)} candles"
        return df, None
    except Exception as exc:
        if ws:
            try:
                ws.close()
            except Exception:
                pass
        return None, f"TV {symbol}: {type(exc).__name__}: {str(exc)[:100]}"


def fetch_market_data(symbol, timeframe):
    config = TIMEFRAMES[timeframe]
    sources = TV_SOURCES_MAP.get(symbol, [f"OANDA:{symbol}"])
    previous_source = source_locks.get((symbol, timeframe))

    ordered_sources = []
    if previous_source in sources:
        ordered_sources.append(previous_source)
    for source in sources:
        if source not in ordered_sources:
            ordered_sources.append(source)

    errors = []
    for source in ordered_sources:
        frame, error = fetch_from_tradingview(
            symbol=source,
            interval=config["interval"],
            n_bars=config["bars"],
            candle_seconds=config["seconds"],
        )
        if frame is not None:
            source_locks[(symbol, timeframe)] = source
            return frame, source, None
        errors.append(error)

    return None, None, " | ".join(errors)


# ============================================================
# MACD & STRATEGIC ENGINE
# ============================================================

def macd_histogram(close, fast, slow, signal):
    fast_ema = close.ewm(span=fast, adjust=False, min_periods=fast).mean()
    slow_ema = close.ewm(span=slow, adjust=False, min_periods=slow).mean()
    macd_line = fast_ema - slow_ema
    signal_line = macd_line.ewm(span=signal, adjust=False, min_periods=signal).mean()
    return macd_line - signal_line


class DivergenceTracker:
    def __init__(self, side="bull", price_window=7):
        self.side = side
        self.price_window = price_window
        self.reset_all()

    def reset_all(self):
        self.phase = None
        self.first_extreme_idx = None
        self.first_hist = None
        self.first_price = None
        self.middle_peak_idx = None
        self.middle_peak_val = None
        self.state = "SEARCHING_FIRST"

    def get_price_extreme(self, df, idx):
        left = max(0, idx - self.price_window)
        segment = df.iloc[left : idx + 1]
        return float(segment["low"].min()) if self.side == "bull" else float(segment["high"].max())

    def update(self, df):
        events = []
        if len(df) < 3:
            return events

        for i in range(2, len(df)):
            h1 = float(df.iloc[i - 1]["h"])
            h0 = float(df.iloc[i - 2]["h"])
            h2 = float(df.iloc[i]["h"])

            phase_now = "negative" if h1 < 0 else "positive" if h1 > 0 else "zero"

            if phase_now != self.phase and phase_now != "zero":
                self.phase = phase_now
                self.reset_all()
                self.phase = phase_now

            if self.side == "bull" and self.phase != "negative":
                continue
            if self.side == "bear" and self.phase != "positive":
                continue

            is_trough = (h1 < 0 and h1 <= h0 and h1 < h2)
            is_peak = (h1 > 0 and h1 >= h0 and h1 > h2)
            is_local_extreme = is_trough if self.side == "bull" else is_peak

            if is_local_extreme:
                ext_price = self.get_price_extreme(df, i - 1)

                if self.state == "SEARCHING_FIRST":
                    if self.first_extreme_idx is None or abs(h1) > abs(self.first_hist):
                        self.first_extreme_idx = i - 1
                        self.first_hist = h1
                        self.first_price = ext_price
                        self.middle_peak_idx = None
                        self.middle_peak_val = None

                elif self.state == "SEARCHING_SECOND":
                    if self.middle_peak_idx is not None and (i - 1) > self.middle_peak_idx:
                        macd_condition = abs(h1) < abs(self.first_hist)
                        price_condition = (ext_price < self.first_price) if self.side == "bull" else (ext_price > self.first_price)

                        if macd_condition and price_condition:
                            event_type = "DIVERGENCE_BULL" if self.side == "bull" else "DIVERGENCE_BEAR"
                            events.append({
                                "type": event_type,
                                "side": self.side,
                                "time": df.index[i - 1].isoformat(),
                                "message": f"{event_type} | H1={self.first_hist:.4f}, H2={h1:.4f}, Price1={self.first_price}, Price2={ext_price}"
                            })
                            self.reset_all()
                            self.phase = phase_now

            if self.first_extreme_idx is not None and (i - 1) > self.first_extreme_idx:
                if self.side == "bull":
                    if self.middle_peak_val is None or h1 > self.middle_peak_val:
                        self.middle_peak_val = h1
                        self.middle_peak_idx = i - 1
                        if self.state == "SEARCHING_FIRST":
                            self.state = "SEARCHING_SECOND"
                else:
                    if self.middle_peak_val is None or h1 < self.middle_peak_val:
                        self.middle_peak_val = h1
                        self.middle_peak_idx = i - 1
                        if self.state == "SEARCHING_FIRST":
                            self.state = "SEARCHING_SECOND"

        return events


class StrategyEngine:
    def __init__(self):
        self.trackers = {}
        self.ready_state = {}
        self.fire_armed = {}
        self.last_bar_time = {}
        self.fire_history = {}
        self.last_fires = {}
        self.warmed_up = set()

    def get_last_fire(self, symbol):
        return self.last_fires.get(symbol)

    def process(self, symbol, timeframe, candles, is_warmup=False):
        df = candles.copy().sort_index()
        if len(df) < 150:
            return []

        emitted = []
        for side in ["bull", "bear"]:
            key = (symbol, timeframe, side)
            
            h1 = macd_histogram(df["close"], 48, 104, 36)
            work = df.copy()
            work["h"] = h1
            tracker = self.trackers.setdefault(key, DivergenceTracker(side=side, price_window=PRICE_MATCH_WINDOW))
            
            valid_work = work.dropna(subset=["h"])
            for event in tracker.update(valid_work):
                if not is_warmup:
                    emitted.append(event)

            if timeframe == "M1":
                h2 = macd_histogram(df["close"], 12, 26, 9)
                h3 = macd_histogram(df["close"], 3, 6, 2)
                
                aligned = pd.DataFrame({"h2": h2, "h3": h3}).dropna()
                if len(aligned) < 3:
                    continue

                last_time = self.last_bar_time.get(key)
                new_rows = aligned if is_warmup else (aligned.iloc[-2:] if last_time is None else aligned.loc[aligned.index > last_time])

                history = self.fire_history.setdefault(key, [])
                previous_h2 = None

                for timestamp, row in new_rows.iterrows():
                    now2 = float(row["h2"])
                    now3 = float(row["h3"])

                    if previous_h2 is None:
                        pos = aligned.index.get_loc(timestamp)
                        if pos > 0:
                            previous_h2 = float(aligned.iloc[pos - 1]["h2"])

                    is_ready_condition = (previous_h2 is not None and previous_h2 <= 0 < now2) if side == "bull" else (previous_h2 is not None and previous_h2 >= 0 > now2)

                    if is_ready_condition:
                        if not self.ready_state.get(key, False):
                            self.ready_state[key] = True
                            self.fire_armed[key] = False
                            history.clear()
                            if not is_warmup:
                                emitted.append({
                                    "type": "READY",
                                    "side": side,
                                    "time": timestamp.isoformat(),
                                    "message": f"READY: MACD(12,26,9) crossed zero ({side.upper()})."
                                })

                    previous_h2 = now2

                    if self.ready_state.get(key, False):
                        if (side == "bull" and now3 > 0) or (side == "bear" and now3 < 0):
                            self.ready_state[key] = False
                            self.fire_armed[key] = False

                        if side == "bull" and len(history) >= 1 and history[-1] >= 0 and now3 < 0:
                            self.fire_armed[key] = True
                            history[:] = [now3]
                        elif side == "bull" and self.fire_armed.get(key, False):
                            if len(history) >= 2 and history[-1] < 0 and history[-1] <= history[-2] and history[-1] < now3:
                                fire_event = {
                                    "type": "FIRE",
                                    "side": side,
                                    "time": timestamp.isoformat(),
                                    "message": f"FIRE: First confirmed negative extremum MACD(3,6,2) (BULL)."
                                }
                                self.last_fires[symbol] = fire_event
                                if not is_warmup:
                                    emitted.append(fire_event)
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
# EVENT REGISTRATION & TELEGRAM ALERTS
# ============================================================

def register_event(symbol, timeframe, event):
    event_time = pd.Timestamp(event["time"])
    if event_time.tzinfo is None:
        event_time = event_time.tz_localize("UTC")
    event_time = event_time.tz_convert("UTC")

    event_id = f"{symbol}|{timeframe}|{event['type']}|{event.get('side', '')}|{event_time.isoformat()}"
    with state_lock:
        if event_id in seen_event_ids:
            return
        seen_event_ids.add(event_id)
        item = {
            "id": event_id,
            "symbol": symbol,
            "timeframe": timeframe,
            "type": event["type"],
            "side": event.get("side", ""),
            "time": event_time.isoformat(),
            "message": event["message"],
        }
        event_history.append(item)
        event_history[:] = event_history[-1000:]
        save_state()

    alert = (
        f"🚨 <b>سیگنال جدید بازار</b>\n"
        f"🏷️ نماد: <b>{symbol}</b> ({timeframe})\n"
        f"📊 نوع رویداد: <b>{event['type']}</b> ({event.get('side', '')})\n"
        f"🕒 زمان (به‌وقت تهران): <b>{format_time(event_time)}</b>\n\n"
        f"💬 {event['message']}"
    )
    broadcast(alert)
    log.info("EVENT: %s", alert)


def build_report():
    events = event_history[-20:]
    lines = ["<b>MACD MULTI-ENGINE REPORT (Tehran Time)</b>", ""]
    for item in sorted(events, key=lambda x: x["time"], reverse=True)[:10]:
        lines.append(f"• {item['symbol']} | {item['timeframe']} | {item['type']} ({item.get('side', '')}) | {format_time(item['time'])}")
    return "\n".join(lines)


# ============================================================
# SCANNER LOOP & WARM-UP
# ============================================================

def warmup_symbol(symbol):
    for timeframe in TIMEFRAMES:
        frame, _, _ = fetch_market_data(symbol, timeframe)
        if frame is not None:
            engine.process(symbol, timeframe, frame, is_warmup=True)
    engine.warmed_up.add(symbol)
    log.info("Warm-up completed for %s", symbol)


def scan_symbol(symbol):
    if symbol not in engine.warmed_up:
        warmup_symbol(symbol)
        return

    for timeframe in TIMEFRAMES:
        frame, _, _ = fetch_market_data(symbol, timeframe)
        if frame is not None:
            for event in engine.process(symbol, timeframe, frame, is_warmup=False):
                register_event(symbol, timeframe, event)


def scanner_loop():
    global scanner_started
    log.info("Robust Multi-Symbol MACD scanner started.")
    scanner_started = True

    while True:
        for symbol in SYMBOLS:
            try:
                scan_symbol(symbol)
            except Exception:
                log.exception("Error scanning %s", symbol)
        time.sleep(SCAN_INTERVAL)


# ============================================================
# FLASK & STARTUP
# ============================================================

@app.get("/")
def index():
    return jsonify({"status": "running", "symbols": SYMBOLS})


@app.get("/report")
def report():
    return app.response_class(build_report(), mimetype="text/html")


def start_background_threads():
    threading.Thread(target=scanner_loop, daemon=True, name="scanner").start()
    if bot:
        threading.Thread(target=telegram_polling, daemon=True, name="telegram").start()


if __name__ == "__main__":
    start_background_threads()
    app.run(host=HOST, port=PORT, debug=False, use_reloader=False, threaded=True)
