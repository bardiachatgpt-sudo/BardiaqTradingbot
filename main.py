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

try:
    from biquote import Biquote
    HAS_BIQUOTE = True
except ImportError:
    HAS_BIQUOTE = False
    Biquote = None

try:
    from tvDatafeed import TvDatafeed, Interval as TVInterval
    HAS_TVDATAFEED = True
except ImportError:
    HAS_TVDATAFEED = False
    TvDatafeed = None
    TVInterval = None


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

PROVIDER_COOLDOWN = int(os.getenv("PROVIDER_COOLDOWN_SECONDS", "300"))

STATE_FILE = os.getenv("STATE_FILE", "scanner_state.json")
PRICE_MATCH_WINDOW = int(os.getenv("PRICE_MATCH_WINDOW", "4"))

TWELVE_DATA_API_KEY = os.getenv("TWELVE_DATA_API_KEY", "").strip()

TEHRAN = pytz.timezone("Asia/Tehran")
SYMBOL = os.getenv("SYMBOL", "XAUUSD").strip().upper()

TIMEFRAMES = {
    "M1": {"interval": "1", "bars": 1440, "seconds": 60},
    "M5": {"interval": "5", "bars": 150, "seconds": 300},
}

TV_SOURCES = {
    "XAUUSD": [
        "FX_IDC:XAUUSD", "OANDA:XAUUSD", "FOREXCOM:XAUUSD", "BINANCE:PAXGUSDT"
    ],
    "EURUSD": ["FX:EURUSD", "FX_IDC:EURUSD", "OANDA:EURUSD"],
    "GBPUSD": ["FX:GBPUSD", "OANDA:GBPUSD"],
    "USDJPY": ["FX:USDJPY", "OANDA:USDJPY"],
}.get(SYMBOL, [f"OANDA:{SYMBOL}"])

# Provider symbol mapping
PROVIDER_SYMBOLS = {
    "XAUUSD": {
        "yahoo": "GC=F",
        "biquote": "XAUUSD",
        "xaus": "gold",
        "twelvedata": "XAU/USD",
    },
    "EURUSD": {
        "yahoo": "EURUSD=X",
        "biquote": "EURUSD",
        "xaus": None,
        "twelvedata": "EUR/USD",
    },
    "GBPUSD": {
        "yahoo": "GBPUSD=X",
        "biquote": "GBPUSD",
        "xaus": None,
        "twelvedata": "GBP/USD",
    },
    "USDJPY": {
        "yahoo": "JPY=X",
        "biquote": "USDJPY",
        "xaus": None,
        "twelvedata": "USD/JPY",
    },
}

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

_provider_failures = {}
_provider_cooldown = {}

scanner_started = False


# ============================================================
# CIRCUIT BREAKER (Provider Health)
# ============================================================

def _should_skip(timeframe, provider):
    return time.time() < _provider_cooldown.get((timeframe, provider), 0)


def _mark_failure(timeframe, provider):
    key = (timeframe, provider)
    fails = _provider_failures.get(key, 0) + 1
    _provider_failures[key] = fails
    if fails >= 3:
        _provider_cooldown[key] = time.time() + PROVIDER_COOLDOWN
        log.warning(
            "Provider '%s' disabled for %s for %ss (fails=%d)",
            provider, timeframe, PROVIDER_COOLDOWN, fails,
        )


def _mark_success(timeframe, provider):
    _provider_failures.pop((timeframe, provider), None)
    _provider_cooldown.pop((timeframe, provider), None)


# ============================================================
# STATE
# ============================================================

def load_state():
    global subscribed_users, event_history, seen_event_ids
    try:
        if not os.path.exists(STATE_FILE):
            return
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        subscribed_users = set(data.get("users", []))
        event_history = data.get("events", [])
        seen_event_ids = {i.get("id") for i in event_history if i.get("id")}
    except Exception:
        log.exception("State loading failed")


def save_state():
    try:
        with state_lock:
            payload = {
                "users": list(subscribed_users),
                "events": event_history[-1000:],
            }
            tmp = STATE_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            os.replace(tmp, STATE_FILE)
    except Exception:
        log.exception("State saving failed")


load_state()


# ============================================================
# FORMATTING
# ============================================================

def utc_now():
    return datetime.now(timezone.utc)


def to_tehran(value):
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    return ts.tz_convert(TEHRAN)


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
            bot.send_message(target, message, disable_web_page_preview=True)
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
            "/report برای گزارش، /status برای وضعیت.",
            message.chat.id,
        )

    @bot.message_handler(commands=["stop", "unsubscribe"])
    def telegram_stop(message):
        with state_lock:
            subscribed_users.discard(message.chat.id)
            save_state()
        send_message("ارسال هشدار متوقف شد.", message.chat.id)

    @bot.message_handler(commands=["report", "status"])
    def telegram_report(message):
        try:
            send_message(build_report(), message.chat.id)
        except Exception:
            log.exception("Report command failed")


def telegram_polling():
    if not bot:
        return
    try:
        me = bot.get_me()
        log.info("Telegram connected: @%s", me.username)
    except Exception as exc:
        log.error("Telegram get_me failed: %s", exc)
        return

    try:
        bot.delete_webhook(drop_pending_updates=True)
    except Exception:
        pass

    retry_delay = 5
    while True:
        try:
            bot.infinity_polling(
                skip_pending=True, timeout=20, long_polling_timeout=20
            )
        except Exception as exc:
            text = str(exc)
            if "409" in text or "Conflict" in text:
                log.error("Telegram 409 conflict; retry in %ss", retry_delay)
            elif "401" in text or "Unauthorized" in text:
                log.error("Invalid BOT_TOKEN; stopping.")
                return
            else:
                log.exception("Telegram polling error")
            time.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 60)
            continue
        retry_delay = 5
        time.sleep(2)


# ============================================================
# PROVIDER 1: TradingView (Raw WebSocket)
# ============================================================

HEARTBEAT_PATTERN = re.compile(r"~m~\d+~m~~h~\d+")


def _tv_send(ws, method, params):
    payload = json.dumps({"m": method, "p": params}, separators=(",", ":"))
    ws.send(f"~m~{len(payload)}~m~{payload}")


def _extract_tv_messages(raw):
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


def _extract_ohlc_records(obj):
    records = []
    if isinstance(obj, dict):
        values = obj.get("v")
        if isinstance(values, list) and len(values) >= 5:
            try:
                ts = float(values[0])
                if ts > 100000000:
                    r = {
                        "time": int(ts),
                        "open": float(values[1]),
                        "high": float(values[2]),
                        "low": float(values[3]),
                        "close": float(values[4]),
                    }
                    if all(np.isfinite(r[k]) for k in ("open", "high", "low", "close")) and r["high"] >= r["low"]:
                        records.append(r)
            except (TypeError, ValueError, OverflowError):
                pass
        for v in obj.values():
            records.extend(_extract_ohlc_records(v))
    elif isinstance(obj, list):
        for item in obj:
            records.extend(_extract_ohlc_records(item))
    return records


def fetch_from_tradingview(symbol, interval, n_bars, candle_seconds):
    ws = None
    bars_by_time = {}
    errors = []

    try:
        ws = websocket.create_connection(
            "wss://data.tradingview.com/socket.io/websocket?from=chart%2F",
            header=[
                "Origin: https://www.tradingview.com",
                "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            ],
            timeout=12,
            enable_multithread=True,
        )
        ws.settimeout(1)

        chart_sess = "cs_" + uuid.uuid4().hex[:16]
        quote_sess = "qs_" + uuid.uuid4().hex[:16]

        _tv_send(ws, "set_auth_token", ["unauthorized_user_token"])
        _tv_send(ws, "chart_create_session", [chart_sess, ""])
        _tv_send(ws, "quote_create_session", [quote_sess])
        _tv_send(ws, "quote_set_fields", [
            quote_sess, "lp", "lp_time", "volume", "bid", "ask",
            "open_price", "high_price", "low_price", "prev_close_price",
            "ch", "chp", "short_name", "exchange", "description",
            "original_name", "local_name",
        ])
        _tv_send(ws, "switch_timezone", [chart_sess, "Etc/UTC"])

        sym = json.dumps({"symbol": symbol, "adjustment": "splits"}, separators=(",", ":"))
        _tv_send(ws, "resolve_symbol", [chart_sess, "symbol_1", "=" + sym])
        _tv_send(ws, "create_series", [
            chart_sess, "series_1", "s1", "symbol_1",
            str(interval), int(n_bars), "",
        ])

        start = time.time()
        last_data = None
        debugged = 0

        while time.time() - start < TV_TIMEOUT:
            try:
                raw = ws.recv()
            except websocket.WebSocketTimeoutException:
                if bars_by_time and last_data and time.time() - last_data >= TV_IDLE_TIMEOUT:
                    break
                continue
            except Exception as exc:
                errors.append(str(exc))
                break

            if not raw:
                continue
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", errors="ignore")

            for hb in HEARTBEAT_PATTERN.findall(raw):
                try:
                    ws.send(hb)
                except Exception:
                    errors.append("heartbeat send failed")
                    ws = None
                    break
            if ws is None:
                break

            for msg in _extract_tv_messages(raw):
                method = msg.get("m")
                params = msg.get("p", [])

                if method in ("symbol_error", "series_error", "critical_error"):
                    errors.append(str(params)[:120])
                    continue

                if method in ("timescale_update", "du"):
                    found = []
                    for p in params:
                        found.extend(_extract_ohlc_records(p))
                    if found:
                        for r in found:
                            bars_by_time[r["time"]] = r
                        last_data = time.time()
                    elif debugged < 1:
                        debugged += 1
                        log.warning("TV %s: empty update %s", symbol, str(params)[:200])
                elif method == "series_completed" and bars_by_time:
                    last_data = time.time()

        if ws:
            try:
                ws.close()
            except Exception:
                pass

        if not bars_by_time:
            return None, f"{symbol}: {'; '.join(errors) if errors else 'no OHLC'}"

        df = pd.DataFrame(list(bars_by_time.values())).sort_values("time")
        df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
        df = df.drop_duplicates(subset=["time"], keep="last").set_index("time")
        df = df[["open", "high", "low", "close"]].astype(float)

        now = pd.Timestamp.now(tz="UTC")
        df = df[df.index <= now - pd.Timedelta(seconds=candle_seconds)]
        df = df[np.isfinite(df).all(axis=1)]
        df = df[(df["high"] >= df["low"]) & (df["high"] >= df["open"]) & (df["high"] >= df["close"]) & (df["low"] <= df["open"]) & (df["low"] <= df["close"])]
        df = df.sort_index()

        if len(df) > n_bars:
            df = df.iloc[-n_bars:]
        if len(df) < 150:
            return None, f"{symbol}: only {len(df)} candles"
        return df, None

    except Exception as exc:
        if ws:
            try:
                ws.close()
            except Exception:
                pass
        return None, f"{symbol}: {type(exc).__name__}: {str(exc)[:150]}"


# ============================================================
# PROVIDER 2: TradingView via tvdatafeed
# ============================================================

def fetch_from_tvdatafeed(symbol, interval, n_bars):
    if not HAS_TVDATAFEED:
        return None, "tvdatafeed not installed"

    tv_sym = symbol.split(":", 1)[-1] if ":" in symbol else symbol
    exchange = symbol.split(":", 1)[0] if ":" in symbol else "OANDA"

    interval_map = {
        "1": TVInterval.in_1_minute,
        "5": TVInterval.in_5_minute,
        "15": TVInterval.in_15_minute,
        "60": TVInterval.in_1_hour,
    }
    tv_interval = interval_map.get(str(interval))
    if not tv_interval:
        return None, f"tvdatafeed unsupported interval {interval}"

    try:
        tv = TvDatafeed()
        df = tv.get_hist(
            symbol=tv_sym,
            exchange=exchange,
            interval=tv_interval,
            n_bars=n_bars,
        )
    except Exception as exc:
        return None, f"tvdatafeed: {type(exc).__name__}: {str(exc)[:150]}"

    if df is None or df.empty:
        return None, "tvdatafeed: empty"

    df = df.rename(columns={
        "open": "open", "high": "high", "low": "low", "close": "close",
    })
    df = df[["open", "high", "low", "close"]].astype(float).dropna()

    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    else:
        df.index = df.index.tz_convert("UTC")

    df = df.sort_index().tail(n_bars)
    if len(df) < 150:
        return None, f"tvdatafeed: only {len(df)} candles"
    return df, None


# ============================================================
# PROVIDER 3: Yahoo Finance
# ============================================================

def fetch_from_yahoo(symbol, interval, n_bars):
    if not HAS_YFINANCE:
        return None, "yfinance not installed"

    yahoo_symbol = PROVIDER_SYMBOLS.get(symbol, {}).get("yahoo")
    if not yahoo_symbol:
        return None, f"No Yahoo mapping for {symbol}"

    interval_map = {"1": "1m", "5": "5m", "15": "15m", "30": "30m", "60": "1h"}
    yf_interval = interval_map.get(str(interval))
    if not yf_interval:
        return None, f"Yahoo unsupported interval {interval}"

    period = "7d" if yf_interval == "1m" else "60d"

    try:
        df = yf.download(
            yahoo_symbol, period=period, interval=yf_interval,
            progress=False, auto_adjust=False, threads=False,
        )
    except Exception as exc:
        return None, f"yahoo: {type(exc).__name__}: {str(exc)[:150]}"

    if df is None or df.empty:
        return None, "yahoo: empty"

    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    df = df.rename(columns={"Open": "open", "High": "high", "Low": "low", "Close": "close"})
    missing = [c for c in ("open", "high", "low", "close") if c not in df.columns]
    if missing:
        return None, f"yahoo: missing {missing}"

    df = df[["open", "high", "low", "close"]].astype(float).dropna()

    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    else:
        df.index = df.index.tz_convert("UTC")

    df = df.sort_index()
    df = df[df.index < pd.Timestamp.now(tz="UTC")]
    df = df.tail(n_bars)

    if len(df) < 150:
        return None, f"yahoo: only {len(df)} candles"
    return df, None


# ============================================================
# PROVIDER 4: Biquote (REST)
# ============================================================

def fetch_from_biquote(symbol, interval, n_bars):
    bq_symbol = PROVIDER_SYMBOLS.get(symbol, {}).get("biquote")
    if not bq_symbol:
        return None, f"No Biquote mapping for {symbol}"

    interval_map = {"1": "1m", "5": "5m", "15": "15m", "60": "1h"}
    bq_interval = interval_map.get(str(interval))
    if not bq_interval:
        return None, f"Biquote unsupported interval {interval}"

    try:
        url = f"https://biquote.io/api/{bq_symbol}/ohlc"
        resp = requests.get(
            url,
            params={"interval": bq_interval, "limit": n_bars},
            timeout=HTTP_TIMEOUT,
        )
        if resp.status_code != 200:
            return None, f"biquote: HTTP {resp.status_code}"

        data = resp.json()
        bars = data.get("bars") or data.get("data") or []
        if not bars:
            return None, "biquote: empty bars"

        df = pd.DataFrame(bars)

        # Normalize column names (Biquote may use t/o/h/l/c or time/open/...)
        rename = {
            "t": "time", "timestamp": "time",
            "o": "open", "h": "high", "l": "low", "c": "close",
        }
        df = df.rename(columns={k: v for k, v in rename.items() if k in df.columns})

        if "time" not in df.columns:
            return None, f"biquote: no time column (keys={list(df.columns)[:6]})"

        if "open" not in df.columns:
            # Some APIs return a single "ohlc" or "v" array
            return None, f"biquote: unexpected schema (keys={list(df.columns)[:6]})"

        df["time"] = pd.to_datetime(df["time"], unit="s", utc=True, errors="coerce")
        df = df.dropna(subset=["time"]).set_index("time")
        df = df[["open", "high", "low", "close"]].astype(float).dropna()
        df = df.sort_index()
        df = df[~df.index.duplicated(keep="last")].tail(n_bars)

        if len(df) < 150:
            return None, f"biquote: only {len(df)} candles"
        return df, None

    except Exception as exc:
        return None, f"biquote: {type(exc).__name__}: {str(exc)[:150]}"


# ============================================================
# PROVIDER 5: XAUS API
# ============================================================

def fetch_from_xaus(symbol, interval, n_bars):
    xaus_symbol = PROVIDER_SYMBOLS.get(symbol, {}).get("xaus")
    if not xaus_symbol:
        return None, f"No XAUS mapping for {symbol}"

    interval_map = {"1": "1m", "5": "5m", "15": "15m", "60": "1h"}
    xaus_interval = interval_map.get(str(interval))
    if not xaus_interval:
        return None, f"XAUS unsupported interval {interval}"

    # Approximate range needed
    range_map = {"1m": "1d", "5m": "5d", "15m": "15d", "1h": "60d"}
    xaus_range = range_map.get(xaus_interval, "5d")

    try:
        url = "https://xaus-api.onrender.com/api/v1/chart"
        resp = requests.get(
            url,
            params={
                "symbol": xaus_symbol,
                "interval": xaus_interval,
                "range": xaus_range,
            },
            timeout=HTTP_TIMEOUT,
        )
        if resp.status_code != 200:
            return None, f"xaus: HTTP {resp.status_code}"

        data = resp.json()
        # Try common payload shapes
        rows = data.get("data") or data.get("candles") or data.get("ohlc") or []
        if not rows:
            return None, "xaus: empty"

        df = pd.DataFrame(rows)

        rename = {
            "t": "time", "timestamp": "time", "date": "time",
            "o": "open", "h": "high", "l": "low", "c": "close",
        }
        df = df.rename(columns={k: v for k, v in rename.items() if k in df.columns})

        if "time" not in df.columns:
            return None, f"xaus: no time (keys={list(df.columns)[:6]})"

        df["time"] = pd.to_datetime(df["time"], utc=True, errors="coerce")
        df = df.dropna(subset=["time"]).set_index("time")
        df = df[["open", "high", "low", "close"]].astype(float).dropna()
        df = df.sort_index().tail(n_bars)

        if len(df) < 150:
            return None, f"xaus: only {len(df)} candles"
        return df, None

    except Exception as exc:
        return None, f"xaus: {type(exc).__name__}: {str(exc)[:150]}"


# ============================================================
# PROVIDER 6: Twelve Data (requires API key)
# ============================================================

def fetch_from_twelvedata(symbol, interval, n_bars):
    if not TWELVE_DATA_API_KEY:
        return None, "twelvedata: no API key"

    td_symbol = PROVIDER_SYMBOLS.get(symbol, {}).get("twelvedata")
    if not td_symbol:
        return None, f"No TwelveData mapping for {symbol}"

    interval_map = {"1": "1min", "5": "5min", "15": "15min", "60": "1h"}
    td_interval = interval_map.get(str(interval))
    if not td_interval:
        return None, f"TwelveData unsupported interval {interval}"

    try:
        url = "https://api.twelvedata.com/time_series"
        resp = requests.get(
            url,
            params={
                "symbol": td_symbol,
                "interval": td_interval,
                "outputsize": min(n_bars, 5000),
                "apikey": TWELVE_DATA_API_KEY,
                "order": "ASC",
            },
            timeout=HTTP_TIMEOUT,
        )
        if resp.status_code != 200:
            return None, f"twelvedata: HTTP {resp.status_code}"

        data = resp.json()
        if "values" not in data:
            return None, f"twelvedata: {str(data)[:150]}"

        df = pd.DataFrame(data["values"])
        df = df.rename(columns={
            "datetime": "time",
            "open": "open", "high": "high", "low": "low", "close": "close",
        })
        df["time"] = pd.to_datetime(df["time"], utc=True, errors="coerce")
        df = df.dropna(subset=["time"]).set_index("time")
        df = df[["open", "high", "low", "close"]].astype(float).dropna()
        df = df.sort_index().tail(n_bars)

        if len(df) < 150:
            return None, f"twelvedata: only {len(df)} candles"
        return df, None

    except Exception as exc:
        return None, f"twelvedata: {type(exc).__name__}: {str(exc)[:150]}"


# ============================================================
# ORCHESTRATOR
# ============================================================

def fetch_market_data(timeframe):
    config = TIMEFRAMES[timeframe]
    errors = []

    # ---- 1) TradingView raw WebSocket (multiple exchanges) ----
    for source in TV_SOURCES:
        if _should_skip(timeframe, source):
            errors.append(f"{source}: skipped (cooldown)")
            continue

        frame, err = fetch_from_tradingview(
            source, config["interval"], config["bars"], config["seconds"]
        )
        if frame is not None:
            _mark_success(timeframe, source)
            return frame, source, None

        _mark_failure(timeframe, source)
        errors.append(err or f"{source}: unknown")
        log.warning("TV source failed: %s", err)

    # ---- 2) tvdatafeed (TradingView alternative client) ----
    if not _should_skip(timeframe, "tvdatafeed"):
        for source in TV_SOURCES[:2]:  # try top 2 exchanges
            frame, err = fetch_from_tvdatafeed(source, config["interval"], config["bars"])
            if frame is not None:
                _mark_success(timeframe, "tvdatafeed")
                return frame, f"tvdatafeed:{source}", None
            errors.append(err or "tvdatafeed: unknown")
        _mark_failure(timeframe, "tvdatafeed")
        log.warning("tvdatafeed failed")
    else:
        errors.append("tvdatafeed: skipped (cooldown)")

    # ---- 3) Yahoo Finance ----
    if not _should_skip(timeframe, "yahoo"):
        frame, err = fetch_from_yahoo(SYMBOL, config["interval"], config["bars"])
        if frame is not None:
            _mark_success(timeframe, "yahoo")
            return frame, f"yahoo:{PROVIDER_SYMBOLS.get(SYMBOL, {}).get('yahoo')}", None
        _mark_failure(timeframe, "yahoo")
        errors.append(err or "yahoo: unknown")
        log.warning("Yahoo failed: %s", err)
    else:
        errors.append("yahoo: skipped (cooldown)")

    # ---- 4) Biquote ----
    if not _should_skip(timeframe, "biquote"):
        frame, err = fetch_from_biquote(SYMBOL, config["interval"], config["bars"])
        if frame is not None:
            _mark_success(timeframe, "biquote")
            return frame, f"biquote:{PROVIDER_SYMBOLS.get(SYMBOL, {}).get('biquote')}", None
        _mark_failure(timeframe, "biquote")
        errors.append(err or "biquote: unknown")
        log.warning("Biquote failed: %s", err)
    else:
        errors.append("biquote: skipped (cooldown)")

    # ---- 5) XAUS API ----
    if not _should_skip(timeframe, "xaus"):
        frame, err = fetch_from_xaus(SYMBOL, config["interval"], config["bars"])
        if frame is not None:
            _mark_success(timeframe, "xaus")
            return frame, "xaus:gold", None
        _mark_failure(timeframe, "xaus")
        errors.append(err or "xaus: unknown")
        log.warning("XAUS failed: %s", err)
    else:
        errors.append("xaus: skipped (cooldown)")

    # ---- 6) Twelve Data (if key available) ----
    if TWELVE_DATA_API_KEY and not _should_skip(timeframe, "twelvedata"):
        frame, err = fetch_from_twelvedata(SYMBOL, config["interval"], config["bars"])
        if frame is not None:
            _mark_success(timeframe, "twelvedata")
            return frame, f"twelvedata:{PROVIDER_SYMBOLS.get(SYMBOL, {}).get('twelvedata')}", None
        _mark_failure(timeframe, "twelvedata")
        errors.append(err or "twelvedata: unknown")
        log.warning("TwelveData failed: %s", err)
    elif TWELVE_DATA_API_KEY:
        errors.append("twelvedata: skipped (cooldown)")

    return None, None, " | ".join(errors)


# ============================================================
# MACD & DIVERGENCE (unchanged logic)
# ============================================================

def macd_histogram(close, fast, slow, signal):
    fast_ema = close.ewm(span=fast, adjust=False, min_periods=fast).mean()
    slow_ema = close.ewm(span=slow, adjust=False, min_periods=slow).mean()
    macd = fast_ema - slow_ema
    sig = macd.ewm(span=signal, adjust=False, min_periods=signal).mean()
    return macd - sig


class Extreme:
    def __init__(self, index, timestamp, histogram, same_bar_price, window_price, phase_extreme_price):
        self.index = index
        self.time = str(timestamp)
        self.histogram = float(histogram)
        self.same_bar_price = float(same_bar_price)
        self.window_price = float(window_price)
        self.phase_extreme_price = float(phase_extreme_price)


class DivergenceEvent:
    def __init__(self, side, first, middle_index, middle_histogram, second, message):
        self.side = side
        self.first = first
        self.middle_index = middle_index
        self.middle_histogram = middle_histogram
        self.second = second
        self.message = message


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
        seg = df.iloc[left:right + 1]
        row = df.iloc[idx]

        if side == "bull":
            win = float(seg["low"].min())
            same = float(row["low"])
            phase = self.phase_low if self.phase_low is not None else same
        else:
            win = float(seg["high"].max())
            same = float(row["high"])
            phase = self.phase_high if self.phase_high is not None else same

        return Extreme(idx, df.index[idx], float(df.iloc[idx]["h"]), same, win, phase)

    def handle_bull_trough(self, df, idx):
        h = float(df.iloc[idx]["h"])
        if self.phase != "negative":
            return None
        cand = self.price_extreme(df, idx, "bull")
        if self.first is None:
            self.first = cand
            return None
        if abs(h) > abs(self.first.histogram):
            self.first = cand
            self.middle_index = None
            self.middle_histogram = None
            return None
        if self.middle_index is None or self.middle_index <= self.first.index:
            return None
        if abs(h) < abs(self.first.histogram) and cand.window_price < self.first.window_price:
            key = ("bull", self.first.index, idx)
            if key == self.last_event_key:
                return None
            self.last_event_key = key
            ev = DivergenceEvent(
                "bull", self.first, self.middle_index, self.middle_histogram, cand,
                f"BULLISH DIVERGENCE | H1={self.first.histogram:.6f}, H2={h:.6f}, "
                f"Price1={self.first.window_price}, Price2={cand.window_price}"
            )
            self.first = cand
            self.middle_index = None
            self.middle_histogram = None
            return ev
        return None

    def handle_bear_peak(self, df, idx):
        h = float(df.iloc[idx]["h"])
        if self.phase != "positive":
            return None
        cand = self.price_extreme(df, idx, "bear")
        if self.first is None:
            self.first = cand
            return None
        if abs(h) > abs(self.first.histogram):
            self.first = cand
            self.middle_index = None
            self.middle_histogram = None
            return None
        if self.middle_index is None or self.middle_index <= self.first.index:
            return None
        if abs(h) < abs(self.first.histogram) and cand.window_price > self.first.window_price:
            key = ("bear", self.first.index, idx)
            if key == self.last_event_key:
                return None
            self.last_event_key = key
            ev = DivergenceEvent(
                "bear", self.first, self.middle_index, self.middle_histogram, cand,
                f"BEARISH DIVERGENCE | H1={self.first.histogram:.6f}, H2={h:.6f}, "
                f"Price1={self.first.window_price}, Price2={cand.window_price}"
            )
            self.first = cand
            self.middle_index = None
            self.middle_histogram = None
            return ev
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

            self.phase_low = low if self.phase_low is None else min(self.phase_low, low)
            self.phase_high = high if self.phase_high is None else max(self.phase_high, high)

            phase_now = "negative" if h < 0 else "positive" if h > 0 else "zero"

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
                    e = self.handle_bull_trough(df, i - 1)
                    if e:
                        events.append(e)
                if is_peak:
                    e = self.handle_bear_peak(df, i - 1)
                    if e:
                        events.append(e)

            if self.phase == "negative" and self.first is not None and i > self.first.index and h < 0:
                if self.middle_histogram is None or h > self.middle_histogram:
                    self.middle_histogram = h
                    self.middle_index = i

            if self.phase == "positive" and self.first is not None and i > self.first.index and h > 0:
                if self.middle_histogram is None or h < self.middle_histogram:
                    self.middle_histogram = h
                    self.middle_index = i

            self.previous_previous_hist = self.previous_hist
            self.previous_hist = h

        self.last_processed_time = df.index[-1]
        return events


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

        tracker = self.trackers.setdefault(key, DivergenceTracker(PRICE_MATCH_WINDOW))
        emitted = []
        valid_work = work.dropna(subset=["h"])

        for event in tracker.update(valid_work):
            emitted.append({
                "type": "DIVERGENCE_BULL" if event.side == "bull" else "DIVERGENCE_BEAR",
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

            if previous_h2 is not None and previous_h2 <= 0 < now2:
                if not self.ready_state.get(key, False):
                    self.ready_state[key] = True
                    self.fire_armed[key] = False
                    history.clear()
                    emitted.append({
                        "type": "READY",
                        "time": timestamp.isoformat(),
                        "message": "READY: MACD(12,26,9) histogram crossed above zero.",
                    })
            previous_h2 = now2

            if self.ready_state.get(key, False):
                if len(history) >= 1 and history[-1] >= 0 and now3 < 0:
                    self.fire_armed[key] = True
                    history[:] = [now3]
                elif self.fire_armed.get(key, False):
                    if len(history) >= 2:
                        older = history[-2]
                        previous = history[-1]
                        if previous < 0 and previous <= older and previous < now3:
                            emitted.append({
                                "type": "FIRE",
                                "time": timestamp.isoformat(),
                                "message": "FIRE: first confirmed negative MACD(3,6,2) histogram extremum.",
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
# EVENT / REPORT
# ============================================================

def register_event(symbol, timeframe, event):
    event_time = pd.Timestamp(event["time"])
    if event_time.tzinfo is None:
        event_time = event_time.tz_localize("UTC")
    event_time = event_time.tz_convert("UTC")

    event_id = f"{symbol}|{timeframe}|{event['type']}|{event_time.isoformat()}"

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
            ts = pd.Timestamp(item["time"])
            if ts.tzinfo is None:
                ts = ts.tz_localize("UTC")
            if ts.to_pydatetime() >= cutoff:
                result.append(item)
        except Exception:
            continue
    return result


def build_report():
    try:
        events = recent_events(24)
    except Exception:
        log.exception("recent_events failed")
        events = []

    lines = ["MACD DIVERGENCE SCANNER", f"Symbol: {SYMBOL}", "Period: Last 24 Hours", ""]

    for tf in TIMEFRAMES:
        div = sum(1 for e in events if e["timeframe"] == tf and e["type"] in ("DIVERGENCE_BULL", "DIVERGENCE_BEAR"))
        ready = sum(1 for e in events if e["timeframe"] == tf and e["type"] == "READY")
        fire = sum(1 for e in events if e["timeframe"] == tf and e["type"] == "FIRE")

        lines.extend([
            f"Timeframe: {tf}",
            f"Source: {source_locks.get(tf, 'Not connected')}",
            f"Candles: {latest_data.get(tf, {}).get('count', 0)}",
            f"DIVERGENCE: {div}",
            f"READY: {ready}",
            f"FIRE: {fire}",
            "",
        ])

        err = latest_errors.get(tf)
        if err:
            lines.append(f"DATA ERROR: {err[:300]}")
            lines.append("")

    lines.append("Recent Events:")
    if not events:
        lines.append("No recorded events in the last 24 hours.")
    else:
        for e in sorted(events, key=lambda x: x["time"], reverse=True)[:30]:
            lines.append(f"{e['timeframe']} | {e['type']} | {format_time(e['time'])}")
            lines.append(e["message"])
            lines.append("")

    return "\n".join(lines)


# ============================================================
# SCANNER LOOP
# ============================================================

def scan_timeframe(timeframe):
    log.info("Fetching %s candles for %s", timeframe, SYMBOL)

    frame, source, error = fetch_market_data(timeframe)

    if frame is None:
        latest_errors[timeframe] = error
        log.error("ALL PROVIDERS FAILED for %s: %s", timeframe, error)
        return

    latest_errors[timeframe] = None
    prev_source = source_locks.get(timeframe)
    changed = prev_source is not None and prev_source != source
    source_locks[timeframe] = source

    latest_data[timeframe] = {
        "count": len(frame),
        "last_time": frame.index[-1].isoformat(),
        "last_close": float(frame["close"].iloc[-1]),
        "provider": source,
    }

    if changed:
        engine.reset_key((SYMBOL, timeframe))
        log.warning("PROVIDER CHANGED: %s -> %s (%s) — reset strategy", prev_source, source, timeframe)

    log.info("DATA OK | %s | %s | candles=%s | last_close=%s", timeframe, source, len(frame), format_price(frame["close"].iloc[-1]))

    events = engine.process(SYMBOL, timeframe, frame)
    for event in events:
        try:
            register_event(SYMBOL, timeframe, event)
        except Exception:
            log.exception("register_event failed")


def scanner_loop():
    global scanner_started
    log.info("Scanner started (symbol=%s)", SYMBOL)
    scanner_started = True

    try:
        while True:
            start = time.time()

            for tf in TIMEFRAMES:
                try:
                    scan_timeframe(tf)
                except Exception:
                    log.exception("Scanner error on %s", tf)

            try:
                log.info("\n%s", build_report())
            except Exception:
                log.exception("build_report failed")

            try:
                elapsed = time.time() - start
                sleep_time = max(1, SCAN_INTERVAL - elapsed)
            except Exception:
                sleep_time = SCAN_INTERVAL

            time.sleep(sleep_time)
    except BaseException:
        scanner_started = False
        log.exception("scanner_loop terminated")
        raise


# ============================================================
# FLASK
# ============================================================

@app.get("/")
def index():
    return jsonify({
        "service": "MACD Divergence Scanner",
        "symbol": SYMBOL,
        "providers": {
            "tradingview_raw": True,
            "tvdatafeed": HAS_TVDATAFEED,
            "yahoo": HAS_YFINANCE,
            "biquote": HAS_BIQUOTE,
            "xaus": True,
            "twelvedata": bool(TWELVE_DATA_API_KEY),
        },
        "status": "running",
        "timeframes": list(TIMEFRAMES.keys()),
    })


@app.get("/health")
def health():
    provider_health = {}
    for (tf, p), fails in _provider_failures.items():
        cd = _provider_cooldown.get((tf, p), 0)
        provider_health[f"{tf}:{p}"] = {
            "consecutive_failures": fails,
            "cooldown_until": datetime.fromtimestamp(cd, tz=timezone.utc).isoformat() if cd > time.time() else None,
        }

    return jsonify({
        "status": "ok",
        "symbol": SYMBOL,
        "scanner_started": scanner_started,
        "sources": source_locks,
        "latest_data": latest_data,
        "errors": latest_errors,
        "provider_health": provider_health,
    })


@app.get("/report")
def report():
    try:
        return app.response_class(build_report(), mimetype="text/plain")
    except Exception:
        log.exception("report endpoint failed")
        return app.response_class("Report failed", mimetype="text/plain", status=500)


@app.get("/events")
def events_endpoint():
    return jsonify(recent_events(24))


# ============================================================
# STARTUP
# ============================================================

def start_background_threads():
    threading.Thread(target=scanner_loop, daemon=True, name="scanner").start()
    if bot:
        threading.Thread(target=telegram_polling, daemon=True, name="telegram").start()


if __name__ == "__main__":
    log.info("Starting scanner | symbol=%s", SYMBOL)
    log.info("Providers: yfinance=%s, biquote=%s, tvdatafeed=%s, twelvedata=%s",
             HAS_YFINANCE, HAS_BIQUOTE, HAS_TVDATAFEED, bool(TWELVE_DATA_API_KEY))
    start_background_threads()
    app.run(host=HOST, port=PORT, debug=False, use_reloader=False, threaded=True)
