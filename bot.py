import os
import re
import json
import time
import html
import sqlite3
import random
import string
import threading
import logging
from datetime import datetime, timedelta, timezone
from collections import deque

import numpy as np
import pandas as pd
import pytz
import websocket
import telebot

from flask import Flask

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

app = Flask(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
bot = telebot.TeleBot(BOT_TOKEN) if BOT_TOKEN else None

DB_PATH = os.getenv("DB_PATH", "scanner.db")
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "12"))
TV_INTERVAL = "1"
LIVE_BARS = int(os.getenv("LIVE_BARS", "300"))
HISTORY_BARS = int(os.getenv("HISTORY_BARS", "1700"))
EXTREMA_WINDOW = int(os.getenv("EXTREMA_WINDOW", "3"))
PROMINENCE_FACTOR = float(os.getenv("PROMINENCE_FACTOR", "0.0"))
TEHRAN = pytz.timezone("Asia/Tehran")

SYMBOLS_RAW = os.getenv(
    "SYMBOLS",
    "XAUUSD=OANDA:XAUUSD,"
    "EURUSD=OANDA:EURUSD,"
    "GBPUSD=OANDA:GBPUSD,"
    "USDJPY=OANDA:USDJPY,"
    "AUDUSD=OANDA:AUDUSD,"
    "USDCHF=OANDA:USDCHF,"
    "USDCAD=OANDA:USDCAD"
)

SYMBOLS = {}

for item in SYMBOLS_RAW.split(","):
    if "=" in item:
        name, source = item.strip().split("=", 1)
        SYMBOLS[name.strip().upper()] = source.strip()

if not SYMBOLS:
    SYMBOLS = {"XAUUSD": "OANDA:XAUUSD"}

DB_LOCK = threading.RLock()
ENGINE_LOCK = threading.RLock()
ENGINES = {}
LAST_SEEN = {}
SOURCE_STATUS = {}
SUBSCRIBED_USERS = set()
RUNNING = True


def db():
    connection = sqlite3.connect(DB_PATH, timeout=30)
    connection.row_factory = sqlite3.Row
    return connection


def init_db():
    with DB_LOCK:
        con = db()
        cur = con.cursor()

        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                chat_id INTEGER PRIMARY KEY
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                direction TEXT NOT NULL,
                stage TEXT NOT NULL,
                event_time TEXT NOT NULL,
                details TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(symbol, direction, stage, event_time)
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS runtime (
                symbol TEXT PRIMARY KEY,
                last_seen TEXT,
                source TEXT,
                updated_at TEXT
            )
        """)

        con.commit()

        for row in cur.execute("SELECT chat_id FROM users"):
            SUBSCRIBED_USERS.add(int(row["chat_id"]))

        for row in cur.execute(
            "SELECT symbol, last_seen, source FROM runtime"
        ):
            if row["last_seen"]:
                LAST_SEEN[row["symbol"]] = pd.Timestamp(row["last_seen"])
            SOURCE_STATUS[row["symbol"]] = row["source"] or ""

        con.close()


def save_runtime(symbol, last_seen, source):
    with DB_LOCK:
        con = db()
        con.execute("""
            INSERT INTO runtime(symbol, last_seen, source, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(symbol) DO UPDATE SET
                last_seen=excluded.last_seen,
                source=excluded.source,
                updated_at=excluded.updated_at
        """, (
            symbol,
            last_seen.isoformat() if last_seen is not None else None,
            source,
            datetime.now(timezone.utc).isoformat()
        ))
        con.commit()
        con.close()


def save_user(chat_id):
    with DB_LOCK:
        con = db()
        con.execute(
            "INSERT OR IGNORE INTO users(chat_id) VALUES (?)",
            (chat_id,)
        )
        con.commit()
        con.close()
        SUBSCRIBED_USERS.add(chat_id)


def save_event(symbol, direction, stage, event_time, details=None):
    timestamp = pd.Timestamp(event_time).isoformat()

    with DB_LOCK:
        con = db()
        cur = con.cursor()

        cur.execute("""
            INSERT OR IGNORE INTO events
            (symbol, direction, stage, event_time, details, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            symbol,
            direction,
            stage,
            timestamp,
            json.dumps(details or {}, ensure_ascii=False),
            datetime.now(timezone.utc).isoformat()
        ))

        inserted = cur.rowcount > 0
        con.commit()
        con.close()

    return inserted


def get_events(symbol, hours=24):
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)

    with DB_LOCK:
        con = db()
        rows = con.execute("""
            SELECT * FROM events
            WHERE symbol = ? AND event_time >= ?
            ORDER BY event_time ASC
        """, (symbol, cutoff.isoformat())).fetchall()
        con.close()

    return [dict(row) for row in rows]


def format_price(symbol, value):
    digits = 2 if "JPY" in symbol.upper() else 4
    return f"{float(value):.{digits}f}"


def tehran_time(value):
    timestamp = pd.Timestamp(value)

    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize("UTC")
    else:
        timestamp = timestamp.tz_convert("UTC")

    return timestamp.tz_convert("Asia/Tehran").strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def tv_send(ws, function, params):
    payload = json.dumps(
        {"m": function, "p": params},
        separators=(",", ":")
    )
    ws.send(f"~m~{len(payload)}~m~{payload}")


def parse_tv_messages(raw):
    messages = []

    for part in re.split(r"~m~\d+~m~", raw):
        part = part.strip()

        if not part or part.startswith("~h~"):
            continue

        try:
            messages.append(json.loads(part))
        except Exception:
            continue

    return messages


def fetch_tradingview_candles(
    symbol,
    interval="1",
    n_bars=300,
    timeout_seconds=18
):
    ws = None
    bars = {}

    try:
        ws = websocket.create_connection(
            "wss://data.tradingview.com/socket.io/websocket?from=chart%2F",
            header={
                "Origin": "https://www.tradingview.com",
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 Chrome/120.0 Safari/537.36"
                )
            },
            timeout=5
        )

        chart_session = "cs_" + "".join(
            random.choices(
                string.ascii_lowercase + string.digits,
                k=12
            )
        )

        tv_send(ws, "set_auth_token", ["unauthorized_user_token"])
        tv_send(ws, "chart_create_session", [chart_session, ""])
        tv_send(
            ws,
            "resolve_symbol",
            [
                chart_session,
                "symbol_1",
                json.dumps(
                    {"symbol": symbol, "adjustment": "splits"},
                    separators=(",", ":")
                )
            ]
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
                ""
            ]
        )

        start = time.time()

        while time.time() - start < timeout_seconds:
            try:
                raw = ws.recv()
            except websocket.WebSocketTimeoutException:
                continue

            if not raw:
                continue

            if raw.startswith("~h~"):
                ws.send(raw)
                continue

            for message in parse_tv_messages(raw):
                message_type = message.get("m")
                params = message.get("p", [])

                if message_type not in ("timescale_update", "du"):
                    continue

                if len(params) < 2 or not isinstance(params[1], dict):
                    continue

                series = params[1].get("series_1", {})
                entries = series.get("s", [])

                for entry in entries:
                    values = entry.get("v", [])

                    if len(values) < 5:
                        continue

                    try:
                        timestamp = int(values[0])
                        bars[timestamp] = {
                            "Time": timestamp,
                            "Open": float(values[1]),
                            "High": float(values[2]),
                            "Low": float(values[3]),
                            "Close": float(values[4])
                        }
                    except (ValueError, TypeError):
                        continue

        if ws:
            ws.close()

        if not bars:
            return None, f"No candles returned for {symbol}"

        frame = pd.DataFrame(list(bars.values()))
        frame["Time"] = pd.to_datetime(
            frame["Time"],
            unit="s",
            utc=True
        )

        frame = frame.set_index("Time").sort_index()
        frame = frame[~frame.index.duplicated(keep="last")]

        return frame, None

    except Exception as exc:
        if ws:
            try:
                ws.close()
            except Exception:
                pass

        return None, f"{symbol}: {str(exc)[:180]}"


def calculate_macd(close, fast, slow, signal):
    fast_line = close.ewm(
        span=fast,
        adjust=False,
        min_periods=fast
    ).mean()

    slow_line = close.ewm(
        span=slow,
        adjust=False,
        min_periods=slow
    ).mean()

    macd_line = fast_line - slow_line

    signal_line = macd_line.ewm(
        span=signal,
        adjust=False,
        min_periods=signal
    ).mean()

    histogram = macd_line - signal_line

    return histogram


def calculate_indicators(frame):
    result = frame.copy()

    result["H1"] = calculate_macd(
        result["Close"], 48, 104, 36
    )

    result["H2"] = calculate_macd(
        result["Close"], 12, 26, 9
    )

    result["H3"] = calculate_macd(
        result["Close"], 3, 6, 2
    )

    return result


class DirectionState:
    def __init__(self, direction):
        self.direction = direction
        self.phase_active = False
        self.first = None
        self.middle = None
        self.second = None
        self.divergence = None
        self.ready = False
        self.fire = False
        self.last_fire_time = None

    def reset_phase(self):
        self.phase_active = False
        self.first = None
        self.middle = None
        self.second = None

    def reset_cycle(self):
        self.reset_phase()
        self.divergence = None
        self.ready = False
        self.fire = False
        self.last_fire_time = None


class SymbolEngine:
    def __init__(self, symbol):
        self.symbol = symbol
        self.bull = DirectionState("BULLISH")
        self.bear = DirectionState("BEARISH")
        self.history = deque(maxlen=8)
        self.previous_h2 = None
        self.previous_h3 = None
        self.events = []

    def emit(self, direction, stage, timestamp, details=None):
        event = {
            "symbol": self.symbol,
            "direction": direction,
            "stage": stage,
            "time": pd.Timestamp(timestamp),
            "details": details or {}
        }

        self.events.append(event)
        return event

    def pivot(self):
        if len(self.history) < 3:
            return None

        left, middle, right = list(self.history)[-3:]

        h_left = left["H1"]
        h_mid = middle["H1"]
        h_right = right["H1"]

        if not np.isfinite([h_left, h_mid, h_right]).all():
            return None

        if h_mid < h_left and h_mid <= h_right:
            return "LOW", middle

        if h_mid > h_left and h_mid >= h_right:
            return "HIGH", middle

        return None

    def update_divergence(self, candle, pivot):
        state = self.bull
        direction = "BULLISH"

        if candle["H1"] >= 0:
            state.reset_phase()
        else:
            state.phase_active = True

        if pivot:
            kind, point = pivot

            if kind == "LOW" and point["H1"] < 0:
                if state.first is None:
                    state.first = point
                    state.middle = None
                    state.second = None

                elif point["H1"] <= state.first["H1"]:
                    state.first = point
                    state.middle = None
                    state.second = None

                elif state.middle is not None:
                    state.second = point

                    first_abs = abs(state.first["H1"])
                    second_abs = abs(state.second["H1"])

                    if (
                        second_abs < first_abs
                        and state.second["Low"] < state.first["Low"]
                    ):
                        state.divergence = point
                        state.ready = False
                        state.fire = False

                        self.emit(
                            direction,
                            "DIVERGENCE",
                            point["Time"],
                            {
                                "histogram_first": state.first["H1"],
                                "histogram_second": state.second["H1"],
                                "price_first": state.first["Low"],
                                "price_second": state.second["Low"]
                            }
                        )

            elif (
                kind == "HIGH"
                and point["H1"] < 0
                and state.first is not None
                and point["Time"] > state.first["Time"]
            ):
                if (
                    state.middle is None
                    or abs(point["H1"]) < abs(state.middle["H1"])
                ):
                    state.middle = point

        state = self.bear
        direction = "BEARISH"

        if candle["H1"] <= 0:
            state.reset_phase()
        else:
            state.phase_active = True

        if pivot:
            kind, point = pivot

            if kind == "HIGH" and point["H1"] > 0:
                if state.first is None:
                    state.first = point
                    state.middle = None
                    state.second = None

                elif point["H1"] >= state.first["H1"]:
                    state.first = point
                    state.middle = None
                    state.second = None

                elif state.middle is not None:
                    state.second = point

                    first_abs = abs(state.first["H1"])
                    second_abs = abs(state.second["H1"])

                    if (
                        second_abs < first_abs
                        and state.second["High"] > state.first["High"]
                    ):
                        state.divergence = point
                        state.ready = False
                        state.fire = False

                        self.emit(
                            direction,
                            "DIVERGENCE",
                            point["Time"],
                            {
                                "histogram_first": state.first["H1"],
                                "histogram_second": state.second["H1"],
                                "price_first": state.first["High"],
                                "price_second": state.second["High"]
                            }
                        )

            elif (
                kind == "LOW"
                and point["H1"] > 0
                and state.first is not None
                and point["Time"] > state.first["Time"]
            ):
                if (
                    state.middle is None
                    or abs(point["H1"]) < abs(state.middle["H1"])
                ):
                    state.middle = point

    def update_ready(self, candle):
        h2 = candle["H2"]

        if self.previous_h2 is None or not np.isfinite(h2):
            self.previous_h2 = h2
            return

        if (
            self.bull.divergence is not None
            and not self.bull.ready
            and self.previous_h2 <= 0
            and h2 > 0
        ):
            self.bull.ready = True
            self.emit("BULLISH", "READY", candle["Time"])

        if (
            self.bear.divergence is not None
            and not self.bear.ready
            and self.previous_h2 >= 0
            and h2 < 0
        ):
            self.bear.ready = True
            self.emit("BEARISH", "READY", candle["Time"])

        self.previous_h2 = h2

    def update_fire(self, candle, pivot):
        h3 = candle["H3"]

        if self.previous_h3 is None or not np.isfinite(h3):
            self.previous_h3 = h3
            return

        if self.bull.ready and not self.bull.fire:
            if (
                self.previous_h2 > 0
                and self.previous_h3 >= 0
                and h3 < 0
                and pivot
                and pivot[0] == "LOW"
                and pivot[1]["H3"] < 0
            ):
                self.bull.fire = True
                self.bull.last_fire_time = candle["Time"]

                self.emit(
                    "BULLISH",
                    "FIRE",
                    candle["Time"],
                    {"extremum": pivot[1]["H3"]}
                )

        if self.bear.ready and not self.bear.fire:
            if (
                self.previous_h2 < 0
                and self.previous_h3 <= 0
                and h3 > 0
                and pivot
                and pivot[0] == "HIGH"
                and pivot[1]["H3"] > 0
            ):
                self.bear.fire = True
                self.bear.last_fire_time = candle["Time"]

                self.emit(
                    "BEARISH",
                    "FIRE",
                    candle["Time"],
                    {"extremum": pivot[1]["H3"]}
                )

        self.previous_h3 = h3

    def process(self, row):
        candle = {
            "Time": row.name,
            "Open": float(row["Open"]),
            "High": float(row["High"]),
            "Low": float(row["Low"]),
            "Close": float(row["Close"]),
            "H1": float(row["H1"]),
            "H2": float(row["H2"]),
            "H3": float(row["H3"])
        }

        if not np.isfinite(
            [candle["H1"], candle["H2"], candle["H3"]]
        ).all():
            return []

        self.history.append(candle)
        pivot = self.pivot()

        self.update_divergence(candle, pivot)
        self.update_ready(candle)
        self.update_fire(candle, pivot)

        new_events = self.events[:]
        self.events.clear()

        return new_events


def event_message(event):
    symbol = event["symbol"]
    direction = event["direction"]
    stage = event["stage"]
    timestamp = tehran_time(event["time"])

    direction_text = (
        "صعودی" if direction == "BULLISH" else "نزولی"
    )

    title = {
        "DIVERGENCE": "واگرایی شناسایی شد",
        "READY": "مرحله READY فعال شد",
        "FIRE": "مرحله FIRE ثبت شد"
    }.get(stage, stage)

    details = event.get("details", {})
    lines = [
        f"<b>{html.escape(title)}</b>",
        f"نماد: <code>{html.escape(symbol)}</code>",
        f"جهت: <b>{direction_text}</b>",
        f"مرحله: <code>{stage}</code>",
        f"زمان تهران: <code>{timestamp}</code>"
    ]

    if "histogram_first" in details:
        lines.append(
            "Histogram اول: <code>"
            + str(round(details["histogram_first"], 8))
            + "</code>"
        )
        lines.append(
            "Histogram دوم: <code>"
            + str(round(details["histogram_second"], 8))
            + "</code>"
        )

    return "\n".join(lines)


def broadcast(message):
    if not bot:
        return

    for chat_id in list(SUBSCRIBED_USERS):
        try:
            bot.send_message(
                chat_id,
                message,
                parse_mode="HTML"
            )
        except Exception as exc:
            logging.warning("Telegram delivery error: %s", exc)


def store_and_send(event):
    inserted = save_event(
        event["symbol"],
        event["direction"],
        event["stage"],
        event["time"],
        event.get("details")
    )

    if inserted:
        broadcast(event_message(event))


def build_main_keyboard():
    keyboard = telebot.types.InlineKeyboardMarkup(row_width=2)

    buttons = []

    for symbol in SYMBOLS:
        buttons.append(
            telebot.types.InlineKeyboardButton(
                symbol,
                callback_data=f"symbol:{symbol}"
            )
        )

    keyboard.add(*buttons)

    keyboard.add(
        telebot.types.InlineKeyboardButton(
            "All Symbols Status",
            callback_data="all_status"
        ),
        telebot.types.InlineKeyboardButton(
            "Today's Events",
            callback_data="all_today"
        )
    )

    return keyboard


def build_symbol_keyboard(symbol):
    keyboard = telebot.types.InlineKeyboardMarkup(row_width=2)

    keyboard.add(
        telebot.types.InlineKeyboardButton(
            "Live Status",
            callback_data=f"status:{symbol}"
        ),
        telebot.types.InlineKeyboardButton(
            "Last 24 Hours",
            callback_data=f"history:{symbol}"
        )
    )

    keyboard.add(
        telebot.types.InlineKeyboardButton(
            "Back",
            callback_data="home"
        )
    )

    return keyboard


def symbol_status(symbol):
    events = get_events(symbol, 24)

    counts = {
        "DIVERGENCE": 0,
        "READY": 0,
        "FIRE": 0
    }

    for event in events:
        if event["stage"] in counts:
            counts[event["stage"]] += 1

    source = SOURCE_STATUS.get(symbol, "Unknown")
    latest = events[-1] if events else None

    lines = [
        f"<b>Symbol Report: {html.escape(symbol)}</b>",
        f"Timeframe: <code>M1</code>",
        f"Source: <code>{html.escape(source)}</code>",
        "",
        f"DIVERGENCE: <b>{counts['DIVERGENCE']}</b>",
        f"READY: <b>{counts['READY']}</b>",
        f"FIRE: <b>{counts['FIRE']}</b>"
    ]

    if latest:
        lines.extend([
            "",
            "<b>Latest Recorded Event</b>",
            f"Direction: {latest['direction']}",
            f"Stage: {latest['stage']}",
            f"Tehran time: {tehran_time(latest['event_time'])}"
        ])
    else:
        lines.extend([
            "",
            "No recorded event in the last 24 hours."
        ])

    return "\n".join(lines)


def historical_report(symbol):
    source = SYMBOLS.get(symbol)

    if not source:
        return "Unknown symbol."

    frame, error = fetch_tradingview_candles(
        source,
        "1",
        HISTORY_BARS,
        timeout_seconds=25
    )

    if frame is None:
        return (
            f"Historical data unavailable for {symbol}.\n"
            f"{html.escape(error or '')}"
        )

    frame = calculate_indicators(frame)

    now = pd.Timestamp.now(tz="UTC")
    cutoff = now - pd.Timedelta(hours=24)

    closed = frame.iloc[:-1].copy()
    warmup = closed[closed.index < cutoff]
    target = closed[closed.index >= cutoff]

    if target.empty:
        return (
            f"No closed candles found for {symbol} "
            "inside the requested period."
        )

    engine = SymbolEngine(symbol)
    events = []

    combined = pd.concat([warmup.tail(300), target])

    for _, row in combined.iterrows():
        events.extend(engine.process(row))

    target_start = target.index[0]
    target_end = target.index[-1]

    events = [
        event for event in events
        if target_start <= pd.Timestamp(event["time"]) <= target_end
    ]

    counts = {
        "DIVERGENCE": 0,
        "READY": 0,
        "FIRE": 0
    }

    for event in events:
        counts[event["stage"]] += 1

    lines = [
        f"<b>Historical Report: {html.escape(symbol)}</b>",
        "Timeframe: <code>M1</code>",
        f"Available candles: <b>{len(closed)}</b>",
        f"Candles in 24h window: <b>{len(target)}</b>",
        f"First candle: <code>{tehran_time(target_start)}</code>",
        f"Last candle: <code>{tehran_time(target_end)}</code>",
        "",
        f"DIVERGENCE: <b>{counts['DIVERGENCE']}</b>",
        f"READY: <b>{counts['READY']}</b>",
        f"FIRE: <b>{counts['FIRE']}</b>"
    ]

    if not events:
        lines.append(
            "\nNo complete events detected in the available period."
        )
    else:
        lines.append("\n<b>Detected Events</b>")

        for event in events[-12:]:
            lines.append(
                f"{event['direction']} | {event['stage']} | "
                f"{tehran_time(event['time'])}"
            )

    return "\n".join(lines)


def all_symbols_status():
    lines = ["<b>Scanner Status</b>", ""]

    for symbol in SYMBOLS:
        lines.append(
            f"<b>{html.escape(symbol)}</b>\n"
            f"{html.escape(SOURCE_STATUS.get(symbol, 'Connecting'))}"
        )

    return "\n\n".join(lines)


if bot:

    @bot.message_handler(commands=["start", "menu"])
    def start_handler(message):
        save_user(message.chat.id)

        bot.send_message(
            message.chat.id,
            "MACD Scanner\nSelect a symbol:",
            reply_markup=build_main_keyboard()
        )

    @bot.message_handler(commands=["status"])
    def status_handler(message):
        save_user(message.chat.id)

        bot.send_message(
            message.chat.id,
            all_symbols_status(),
            parse_mode="HTML"
        )

    @bot.callback_query_handler(func=lambda call: True)
    def callback_handler(call):
        data = call.data or ""

        try:
            if data == "home":
                bot.edit_message_text(
                    "MACD Scanner\nSelect a symbol:",
                    call.message.chat.id,
                    call.message.message_id,
                    reply_markup=build_main_keyboard()
                )

            elif data == "all_status":
                bot.send_message(
                    call.message.chat.id,
                    all_symbols_status(),
                    parse_mode="HTML"
                )

            elif data == "all_today":
                for symbol in SYMBOLS:
                    bot.send_message(
                        call.message.chat.id,
                        symbol_status(symbol),
                        parse_mode="HTML"
                    )

            elif data.startswith("symbol:"):
                symbol = data.split(":", 1)[1]

                bot.edit_message_text(
                    f"Selected: {html.escape(symbol)}",
                    call.message.chat.id,
                    call.message.message_id,
                    parse_mode="HTML",
                    reply_markup=build_symbol_keyboard(symbol)
                )

            elif data.startswith("status:"):
                symbol = data.split(":", 1)[1]

                bot.send_message(
                    call.message.chat.id,
                    symbol_status(symbol),
                    parse_mode="HTML"
                )

            elif data.startswith("history:"):
                symbol = data.split(":", 1)[1]

                bot.send_message(
                    call.message.chat.id,
                    f"Scanning the last 24 hours of {symbol}...",
                )

                report = historical_report(symbol)

                bot.send_message(
                    call.message.chat.id,
                    report,
                    parse_mode="HTML"
                )

            bot.answer_callback_query(call.id)

        except Exception as exc:
            logging.exception("Callback error")
            bot.answer_callback_query(
                call.id,
                "Request failed."
            )

    @bot.message_handler(
        func=lambda message: bool(message.text),
        content_types=["text"]
    )
    def text_handler(message):
        text_value = message.text.strip().lower()

        aliases = {
            "طلا": "XAUUSD",
            "gold": "XAUUSD",
            "eurusd": "EURUSD",
            "یورو": "EURUSD",
            "پوند": "GBPUSD",
            "gbpusd": "GBPUSD",
            "ین": "USDJPY",
            "usdjpy": "USDJPY"
        }

        selected = None

        for alias, symbol in aliases.items():
            if alias in text_value and symbol in SYMBOLS:
                selected = symbol
                break

        if selected:
            if any(word in text_value for word in [
                "امروز", "تاریخچه", "گذشته", "24", "۲۴"
            ]):
                bot.send_message(
                    message.chat.id,
                    f"Scanning {selected} history..."
                )

                bot.send_message(
                    message.chat.id,
                    historical_report(selected),
                    parse_mode="HTML"
                )
            else:
                bot.send_message(
                    message.chat.id,
                    symbol_status(selected),
                    parse_mode="HTML",
                    reply_markup=build_symbol_keyboard(selected)
                )
        else:
            bot.send_message(
                message.chat.id,
                "Please select a symbol from the menu or use /start."
            )


def scanner_loop():
    global RUNNING

    while RUNNING:
        for symbol, source in SYMBOLS.items():
            if not RUNNING:
                break

            try:
                frame, error = fetch_tradingview_candles(
                    source,
                    TV_INTERVAL,
                    LIVE_BARS
                )

                if frame is None:
                    SOURCE_STATUS[symbol] = error or "Feed unavailable"
                    logging.warning("%s", SOURCE_STATUS[symbol])
                    continue

                SOURCE_STATUS[symbol] = source

                if len(frame) < 120:
                    logging.warning(
                        "Insufficient candles for %s: %s",
                        symbol,
                        len(frame)
                    )
                    continue

                frame = calculate_indicators(frame)

                closed = frame.iloc[:-1].copy()

                if closed.empty:
                    continue

                latest_closed = closed.index[-1]

                if symbol not in LAST_SEEN:
                    LAST_SEEN[symbol] = latest_closed
                    save_runtime(symbol, latest_closed, source)

                    logging.info(
                        "Live baseline established for %s at %s",
                        symbol,
                        latest_closed
                    )
                    continue

                new_rows = closed[
                    closed.index > LAST_SEEN[symbol]
                ]

                if new_rows.empty:
                    continue

                with ENGINE_LOCK:
                    if symbol not in ENGINES:
                        ENGINES[symbol] = SymbolEngine(symbol)

                    engine = ENGINES[symbol]

                    for timestamp, row in new_rows.iterrows():
                        events = engine.process(row)

                        for event in events:
                            store_and_send(event)

                        LAST_SEEN[symbol] = timestamp

                        save_runtime(
                            symbol,
                            timestamp,
                            source
                        )

            except Exception:
                logging.exception(
                    "Scanner error for %s",
                    symbol
                )

        time.sleep(POLL_SECONDS)


@app.route("/")
def home():
    return {
        "service": "MACD Divergence Scanner",
        "status": "running",
        "timeframe": "M1",
        "symbols": list(SYMBOLS.keys()),
        "timezone": "Asia/Tehran"
    }


@app.route("/health")
def health():
    return {
        "status": "ok",
        "symbols": len(SYMBOLS),
        "telegram": bool(bot)
    }


def run_web():
    port = int(os.getenv("PORT", "8080"))

    app.run(
        host="0.0.0.0",
        port=port,
        debug=False,
        use_reloader=False
    )


def run_telegram():
    if not bot:
        logging.warning("BOT_TOKEN is not configured.")
        return

    while RUNNING:
        try:
            bot.infinity_polling(
                timeout=30,
                long_polling_timeout=30,
                skip_pending=True
            )
        except Exception:
            logging.exception("Telegram polling error")
            time.sleep(5)


def main():
    init_db()

    threading.Thread(
        target=run_web,
        daemon=True
    ).start()

    threading.Thread(
        target=scanner_loop,
        daemon=True
    ).start()

    if bot:
        threading.Thread(
            target=run_telegram,
            daemon=True
        ).start()

    while True:
        time.sleep(60)


if __name__ == "__main__":
    main()
