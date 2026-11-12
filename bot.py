import os
import re
import json
import time
import html
import sqlite3
import random
import string
import logging
import threading

from datetime import datetime, timezone
from collections import deque

import numpy as np
import pandas as pd
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
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "15"))
LIVE_BARS = int(os.getenv("LIVE_BARS", "350"))
HISTORY_BARS = int(os.getenv("HISTORY_BARS", "300"))
TV_TIMEOUT = int(os.getenv("TV_TIMEOUT", "20"))

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


def db_connect():
    connection = sqlite3.connect(DB_PATH, timeout=30)
    connection.row_factory = sqlite3.Row
    return connection


def init_db():
    with DB_LOCK:
        con = db_connect()

        con.execute("""
            CREATE TABLE IF NOT EXISTS users (
                chat_id INTEGER PRIMARY KEY
            )
        """)

        con.execute("""
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

        con.execute("""
            CREATE TABLE IF NOT EXISTS runtime (
                symbol TEXT PRIMARY KEY,
                last_seen TEXT,
                source TEXT,
                updated_at TEXT
            )
        """)

        con.commit()

        rows = con.execute(
            "SELECT chat_id FROM users"
        ).fetchall()

        for row in rows:
            SUBSCRIBED_USERS.add(int(row["chat_id"]))

        con.close()


def save_user(chat_id):
    with DB_LOCK:
        con = db_connect()
        con.execute(
            "INSERT OR IGNORE INTO users(chat_id) VALUES (?)",
            (int(chat_id),)
        )
        con.commit()
        con.close()

    SUBSCRIBED_USERS.add(int(chat_id))


def save_runtime(symbol, timestamp, source):
    timestamp_text = (
        pd.Timestamp(timestamp).isoformat()
        if timestamp is not None
        else None
    )

    with DB_LOCK:
        con = db_connect()

        con.execute("""
            INSERT INTO runtime(
                symbol, last_seen, source, updated_at
            )
            VALUES (?, ?, ?, ?)
            ON CONFLICT(symbol) DO UPDATE SET
                last_seen=excluded.last_seen,
                source=excluded.source,
                updated_at=excluded.updated_at
        """, (
            symbol,
            timestamp_text,
            source,
            datetime.now(timezone.utc).isoformat()
        ))

        con.commit()
        con.close()


def save_event(event):
    event_time = pd.Timestamp(event["time"]).isoformat()

    with DB_LOCK:
        con = db_connect()

        cur = con.execute("""
            INSERT OR IGNORE INTO events(
                symbol,
                direction,
                stage,
                event_time,
                details,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            event["symbol"],
            event["direction"],
            event["stage"],
            event_time,
            json.dumps(
                event.get("details", {}),
                ensure_ascii=False
            ),
            datetime.now(timezone.utc).isoformat()
        ))

        inserted = cur.rowcount > 0

        con.commit()
        con.close()

    return inserted


def get_events(symbol, hours=24):
    cutoff = (
        pd.Timestamp.now(tz="UTC")
        - pd.Timedelta(hours=hours)
    )

    with DB_LOCK:
        con = db_connect()

        rows = con.execute("""
            SELECT *
            FROM events
            WHERE symbol = ?
            ORDER BY event_time DESC
        """, (symbol,)).fetchall()

        con.close()

    result = []

    for row in rows:
        event_time = pd.Timestamp(row["event_time"])

        if event_time.tzinfo is None:
            event_time = event_time.tz_localize("UTC")

        if event_time >= cutoff:
            result.append(dict(row))

    return result


def tehran_time(value):
    timestamp = pd.Timestamp(value)

    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize("UTC")
    else:
        timestamp = timestamp.tz_convert("UTC")

    return timestamp.tz_convert(
        "Asia/Tehran"
    ).strftime("%Y-%m-%d %H:%M:%S")


def format_price(symbol, value):
    digits = 2 if "JPY" in symbol.upper() else 4
    return f"{float(value):.{digits}f}"


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
        except (ValueError, TypeError):
            continue

    return messages


def extract_series_entries(payload):
    if not isinstance(payload, dict):
        return []

    result = []

    for series_name in ("sds_1", "series_1"):
        series = payload.get(series_name)

        if not isinstance(series, dict):
            continue

        entries = series.get("s", [])

        if isinstance(entries, list):
            result.extend(entries)

        updates = series.get("ns", {})

        if isinstance(updates, dict):
            for update in updates.values():
                if isinstance(update, dict):
                    values = update.get("v")

                    if isinstance(values, list):
                        result.append({"v": values})

    return result


def fetch_tradingview_candles(
    symbol,
    interval="1",
    n_bars=350,
    timeout_seconds=20
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

        tv_send(
            ws,
            "set_auth_token",
            ["unauthorized_user_token"]
        )

        tv_send(
            ws,
            "chart_create_session",
            [chart_session, ""]
        )

        tv_send(
            ws,
            "resolve_symbol",
            [
                chart_session,
                "symbol_1",
                json.dumps(
                    {
                        "symbol": symbol,
                        "adjustment": "splits"
                    },
                    separators=(",", ":")
                )
            ]
        )

        tv_send(
            ws,
            "create_series",
            [
                chart_session,
                "sds_1",
                "s1",
                "symbol_1",
                str(interval),
                int(n_bars),
                ""
            ]
        )

        start_time = time.time()

        while time.time() - start_time < timeout_seconds:
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

                if message_type not in (
                    "timescale_update",
                    "du"
                ):
                    continue

                if len(params) < 2:
                    continue

                payload = params[1]

                if not isinstance(payload, dict):
                    continue

                for entry in extract_series_entries(payload):
                    values = entry.get("v", [])

                    if not isinstance(values, list):
                        continue

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

                    except (ValueError, TypeError, IndexError):
                        continue

        ws.close()
        ws = None

        if not bars:
            return None, (
                f"No candles returned for {symbol}. "
                "TradingView returned no usable OHLC data."
            )

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
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass

        return None, f"{symbol}: {str(exc)[:250]}"


def calculate_macd_histogram(close, fast, slow, signal):
    fast_ema = close.ewm(
        span=fast,
        adjust=False,
        min_periods=fast
    ).mean()

    slow_ema = close.ewm(
        span=slow,
        adjust=False,
        min_periods=slow
    ).mean()

    macd_line = fast_ema - slow_ema

    signal_line = macd_line.ewm(
        span=signal,
        adjust=False,
        min_periods=signal
    ).mean()

    return macd_line - signal_line


def calculate_indicators(frame):
    result = frame.copy()

    result["H1"] = calculate_macd_histogram(
        result["Close"],
        48,
        104,
        36
    )

    result["H2"] = calculate_macd_histogram(
        result["Close"],
        12,
        26,
        9
    )

    result["H3"] = calculate_macd_histogram(
        result["Close"],
        3,
        6,
        2
    )

    return result


def confirmed_extremum(history, key):
    if len(history) < 3:
        return None

    left, middle, right = list(history)[-3:]

    a = left[key]
    b = middle[key]
    c = right[key]

    if not np.isfinite([a, b, c]).all():
        return None

    if b < a and b <= c:
        return "LOW", middle

    if b > a and b >= c:
        return "HIGH", middle

    return None


class DirectionState:

    def __init__(self, direction):
        self.direction = direction
        self.first = None
        self.middle = None
        self.second = None
        self.divergence = None
        self.ready = False
        self.fire_armed = False
        self.fire_armed_at = None
        self.fired = False

    def reset_phase(self):
        self.first = None
        self.middle = None
        self.second = None
        self.divergence = None
        self.ready = False
        self.fire_armed = False
        self.fire_armed_at = None
        self.fired = False


class SymbolEngine:

    def __init__(self, symbol):
        self.symbol = symbol

        self.bull = DirectionState("BULLISH")
        self.bear = DirectionState("BEARISH")

        self.h1_history = deque(maxlen=3)
        self.h3_history = deque(maxlen=3)

        self.previous_h2 = None
        self.previous_h3 = None

    def make_event(
        self,
        direction,
        stage,
        timestamp,
        details=None
    ):
        return {
            "symbol": self.symbol,
            "direction": direction,
            "stage": stage,
            "time": pd.Timestamp(timestamp),
            "details": details or {}
        }

    def update_bullish_divergence(self, candle, pivot):
        state = self.bull
        value = candle["H1"]
        events = []

        if value >= 0:
            state.reset_phase()
            return events

        if state.first is None:
            state.first = candle
            return events

        if value <= state.first["H1"]:
            state.first = candle
            state.middle = None
            state.second = None
            state.divergence = None
            state.ready = False
            state.fire_armed = False
            state.fire_armed_at = None
            state.fired = False
            return events

        if (
            pivot
            and pivot[0] == "HIGH"
            and state.first["Time"] < pivot[1]["Time"]
            and pivot[1]["H1"] < 0
        ):
            point = pivot[1]

            if (
                state.middle is None
                or point["H1"] > state.middle["H1"]
            ):
                state.middle = point

        if (
            pivot
            and pivot[0] == "LOW"
            and state.middle is not None
        ):
            point = pivot[1]

            if (
                point["H1"] < 0
                and state.first["Time"] < state.middle["Time"]
                and state.middle["Time"] < point["Time"]
            ):
                state.second = point

                if (
                    abs(point["H1"]) < abs(state.first["H1"])
                    and point["Low"] < state.first["Low"]
                ):
                    state.divergence = point
                    state.ready = False
                    state.fire_armed = False
                    state.fire_armed_at = None
                    state.fired = False

                    events.append(
                        self.make_event(
                            "BULLISH",
                            "DIVERGENCE",
                            point["Time"],
                            {
                                "histogram_first": state.first["H1"],
                                "histogram_second": point["H1"],
                                "price_first": state.first["Low"],
                                "price_second": point["Low"],
                                "middle_histogram": state.middle["H1"]
                            }
                        )
                    )

        return events

    def update_bearish_divergence(self, candle, pivot):
        state = self.bear
        value = candle["H1"]
        events = []

        if value <= 0:
            state.reset_phase()
            return events

        if state.first is None:
            state.first = candle
            return events

        if value >= state.first["H1"]:
            state.first = candle
            state.middle = None
            state.second = None
            state.divergence = None
            state.ready = False
            state.fire_armed = False
            state.fire_armed_at = None
            state.fired = False
            return events

        if (
            pivot
            and pivot[0] == "LOW"
            and state.first["Time"] < pivot[1]["Time"]
            and pivot[1]["H1"] > 0
        ):
            point = pivot[1]

            if (
                state.middle is None
                or point["H1"] < state.middle["H1"]
            ):
                state.middle = point

        if (
            pivot
            and pivot[0] == "HIGH"
            and state.middle is not None
        ):
            point = pivot[1]

            if (
                point["H1"] > 0
                and state.first["Time"] < state.middle["Time"]
                and state.middle["Time"] < point["Time"]
            ):
                state.second = point

                if (
                    abs(point["H1"]) < abs(state.first["H1"])
                    and point["High"] > state.first["High"]
                ):
                    state.divergence = point
                    state.ready = False
                    state.fire_armed = False
                    state.fire_armed_at = None
                    state.fired = False

                    events.append(
                        self.make_event(
                            "BEARISH",
                            "DIVERGENCE",
                            point["Time"],
                            {
                                "histogram_first": state.first["H1"],
                                "histogram_second": point["H1"],
                                "price_first": state.first["High"],
                                "price_second": point["High"],
                                "middle_histogram": state.middle["H1"]
                            }
                        )
                    )

        return events

    def update_ready(self, candle):
        events = []
        h2 = candle["H2"]

        if self.previous_h2 is None:
            self.previous_h2 = h2
            return events

        if not np.isfinite([h2, self.previous_h2]).all():
            self.previous_h2 = h2
            return events

        if (
            self.bull.divergence is not None
            and not self.bull.ready
            and self.previous_h2 <= 0
            and h2 > 0
        ):
            self.bull.ready = True

            events.append(
                self.make_event(
                    "BULLISH",
                    "READY",
                    candle["Time"]
                )
            )

        if (
            self.bear.divergence is not None
            and not self.bear.ready
            and self.previous_h2 >= 0
            and h2 < 0
        ):
            self.bear.ready = True

            events.append(
                self.make_event(
                    "BEARISH",
                    "READY",
                    candle["Time"]
                )
            )

        self.previous_h2 = h2
        return events

    def update_fire(self, candle, pivot):
        events = []
        h3 = candle["H3"]

        if self.previous_h3 is None:
            self.previous_h3 = h3
            return events

        if not np.isfinite([h3, self.previous_h3]).all():
            self.previous_h3 = h3
            return events

        if self.bull.ready and not self.bull.fired:

            if (
                not self.bull.fire_armed
                and self.previous_h3 >= 0
                and h3 < 0
            ):
                self.bull.fire_armed = True
                self.bull.fire_armed_at = candle["Time"]

            if (
                self.bull.fire_armed
                and pivot
                and pivot[0] == "LOW"
                and pivot[1]["H3"] < 0
                and pivot[1]["Time"] >= self.bull.fire_armed_at
            ):
                self.bull.fired = True

                events.append(
                    self.make_event(
                        "BULLISH",
                        "FIRE",
                        candle["Time"],
                        {
                            "extremum_time": str(pivot[1]["Time"]),
                            "histogram_extremum": pivot[1]["H3"]
                        }
                    )
                )

        if self.bear.ready and not self.bear.fired:

            if (
                not self.bear.fire_armed
                and self.previous_h3 <= 0
                and h3 > 0
            ):
                self.bear.fire_armed = True
                self.bear.fire_armed_at = candle["Time"]

            if (
                self.bear.fire_armed
                and pivot
                and pivot[0] == "HIGH"
                and pivot[1]["H3"] > 0
                and pivot[1]["Time"] >= self.bear.fire_armed_at
            ):
                self.bear.fired = True

                events.append(
                    self.make_event(
                        "BEARISH",
                        "FIRE",
                        candle["Time"],
                        {
                            "extremum_time": str(pivot[1]["Time"]),
                            "histogram_extremum": pivot[1]["H3"]
                        }
                    )
                )

        self.previous_h3 = h3
        return events

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

        if not np.isfinite([
            candle["H1"],
            candle["H2"],
            candle["H3"]
        ]).all():
            return []

        self.h1_history.append(candle)
        self.h3_history.append(candle)

        h1_pivot = confirmed_extremum(
            self.h1_history,
            "H1"
        )

        h3_pivot = confirmed_extremum(
            self.h3_history,
            "H3"
        )

        events = []

        events.extend(
            self.update_bullish_divergence(
                candle,
                h1_pivot
            )
        )

        events.extend(
            self.update_bearish_divergence(
                candle,
                h1_pivot
            )
        )

        events.extend(
            self.update_ready(candle)
        )

        events.extend(
            self.update_fire(
                candle,
                h3_pivot
            )
        )

        return events


def event_message(event):
    symbol = event["symbol"]
    direction = event["direction"]
    stage = event["stage"]

    direction_text = (
        "صعودی"
        if direction == "BULLISH"
        else "نزولی"
    )

    stage_text = {
        "DIVERGENCE": "واگرایی شناسایی شد",
        "READY": "مرحله آماده‌باش فعال شد",
        "FIRE": "اکسترمم مرحله نهایی تأیید شد"
    }.get(stage, stage)

    lines = [
        f"<b>{html.escape(stage_text)}</b>",
        f"Symbol: <code>{html.escape(symbol)}</code>",
        f"Direction: <b>{direction_text}</b>",
        f"Stage: <code>{html.escape(stage)}</code>",
        f"Tehran time: <code>{tehran_time(event['time'])}</code>"
    ]

    details = event.get("details", {})

    if "histogram_first" in details:
        lines.extend([
            "First Histogram: <code>"
            + str(round(details["histogram_first"], 8))
            + "</code>",
            "Second Histogram: <code>"
            + str(round(details["histogram_second"], 8))
            + "</code>"
        ])

    if "price_first" in details:
        lines.extend([
            "First Price: <code>"
            + format_price(symbol, details["price_first"])
            + "</code>",
            "Second Price: <code>"
            + format_price(symbol, details["price_second"])
            + "</code>"
        ])

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
            logging.warning(
                "Telegram delivery failed for %s: %s",
                chat_id,
                exc
            )


def store_and_send(event):
    inserted = save_event(event)

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
            "Last 100 Candles",
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

    source = SOURCE_STATUS.get(symbol, "Connecting")

    lines = [
        f"<b>Symbol Report: {html.escape(symbol)}</b>",
        "Timeframe: <code>M1</code>",
        f"Source: <code>{html.escape(source)}</code>",
        "",
        f"DIVERGENCE: <b>{counts['DIVERGENCE']}</b>",
        f"READY: <b>{counts['READY']}</b>",
        f"FIRE: <b>{counts['FIRE']}</b>"
    ]

    if events:
        latest = events[0]

        lines.extend([
            "",
            "<b>Latest Recorded Event</b>",
            f"Direction: {html.escape(latest['direction'])}",
            f"Stage: {html.escape(latest['stage'])}",
            f"Time: <code>{tehran_time(latest['event_time'])}</code>"
        ])
    else:
        lines.append(
            "\nNo recorded events in the last 24 hours."
        )

    return "\n".join(lines)


def historical_report(symbol):
    source = SYMBOLS.get(symbol)

    if not source:
        return "Unknown symbol."

    frame, error = fetch_tradingview_candles(
        source,
        "1",
        HISTORY_BARS,
        TV_TIMEOUT
    )

    if frame is None:
        return (
            f"Historical data unavailable for {symbol}.\n"
            f"{html.escape(error or '')}"
        )

    if frame.empty:
        return f"No candles returned for {symbol}."

    frame = frame.sort_index()
    frame = frame[~frame.index.duplicated(keep="last")]

    frame = calculate_indicators(frame)

    now = pd.Timestamp.now(tz="UTC")

    closed = frame[
        frame.index + pd.Timedelta(minutes=1) <= now
    ].copy()

    if closed.empty:
        return f"No closed candles available for {symbol}."

    target = closed.tail(100).copy()

    engine = SymbolEngine(symbol)
    events = []

    for _, row in closed.iterrows():
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
        if event["stage"] in counts:
            counts[event["stage"]] += 1

    lines = [
        f"<b>Historical Report: {html.escape(symbol)}</b>",
        "Timeframe: <code>M1</code>",
        f"Available closed candles: <b>{len(closed)}</b>",
        f"Analyzed candles: <b>{len(target)}</b>",
        f"First candle: <code>{tehran_time(target_start)}</code>",
        f"Last candle: <code>{tehran_time(target_end)}</code>",
        "",
        f"DIVERGENCE: <b>{counts['DIVERGENCE']}</b>",
        f"READY: <b>{counts['READY']}</b>",
        f"FIRE: <b>{counts['FIRE']}</b>"
    ]

    if len(target) < 100:
        lines.append(
            f"\nWarning: Only {len(target)} closed candles were available."
        )

    if not events:
        lines.append(
            "\nNo complete events detected in the last 100 candles."
        )
    else:
        lines.append("\n<b>Detected Events</b>")

        for event in events[-15:]:
            lines.append(
                f"{event['direction']} | {event['stage']} | "
                f"{tehran_time(event['time'])}"
            )

    return "\n".join(lines)


def all_symbols_status():
    lines = ["<b>Scanner Status</b>", ""]

    for symbol in SYMBOLS:
        status = SOURCE_STATUS.get(symbol, "Connecting")

        lines.append(
            f"<b>{html.escape(symbol)}</b>\n"
            f"{html.escape(status)}"
        )

    return "\n\n".join(lines)


def initialize_live_engine(symbol, frame):
    engine = SymbolEngine(symbol)

    frame = calculate_indicators(frame)

    now = pd.Timestamp.now(tz="UTC")

    closed = frame[
        frame.index + pd.Timedelta(minutes=1) <= now
    ]

    for _, row in closed.iterrows():
        engine.process(row)

    ENGINES[symbol] = engine

    if not closed.empty:
        LAST_SEEN[symbol] = closed.index[-1]

        save_runtime(
            symbol,
            closed.index[-1],
            SYMBOLS[symbol]
        )

    logging.info(
        "Live baseline initialized for %s with %s candles",
        symbol,
        len(closed)
    )


def scanner_loop():
    while RUNNING:

        for symbol, source in SYMBOLS.items():

            if not RUNNING:
                break

            try:
                frame, error = fetch_tradingview_candles(
                    source,
                    "1",
                    LIVE_BARS,
                    TV_TIMEOUT
                )

                if frame is None:
                    SOURCE_STATUS[symbol] = error or "Feed unavailable"

                    logging.warning(
                        "%s",
                        SOURCE_STATUS[symbol]
                    )

                    continue

                SOURCE_STATUS[symbol] = (
                    f"Connected: {len(frame)} candles received"
                )

                if len(frame) < 150:
                    logging.warning(
                        "Insufficient history for %s: %s",
                        symbol,
                        len(frame)
                    )
                    continue

                frame = calculate_indicators(frame)

                now = pd.Timestamp.now(tz="UTC")

                closed = frame[
                    frame.index + pd.Timedelta(minutes=1) <= now
                ].copy()

                if closed.empty:
                    continue

                with ENGINE_LOCK:

                    if symbol not in ENGINES:
                        initialize_live_engine(symbol, frame)
                        continue

                    last_seen = LAST_SEEN.get(symbol)

                    if last_seen is None:
                        initialize_live_engine(symbol, frame)
                        continue

                    new_rows = closed[
                        closed.index > last_seen
                    ]

                    if new_rows.empty:
                        continue

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

            elif data.startswith("symbol:"):
                symbol = data.split(":", 1)[1]

                if symbol not in SYMBOLS:
                    bot.answer_callback_query(
                        call.id,
                        "Unknown symbol."
                    )
                    return

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

                if symbol not in SYMBOLS:
                    bot.answer_callback_query(
                        call.id,
                        "Unknown symbol."
                    )
                    return

                bot.answer_callback_query(
                    call.id,
                    "Historical scan started."
                )

                bot.send_message(
                    call.message.chat.id,
                    f"Scanning the last 100 candles of {symbol}..."
                )

                report = historical_report(symbol)

                bot.send_message(
                    call.message.chat.id,
                    report,
                    parse_mode="HTML"
                )

                return

            bot.answer_callback_query(call.id)

        except Exception:
            logging.exception("Telegram callback error")

            try:
                bot.answer_callback_query(
                    call.id,
                    "Request failed."
                )
            except Exception:
                pass

    @bot.message_handler(
        content_types=["text"],
        func=lambda message: bool(message.text)
    )
    def text_handler(message):
        text_value = message.text.strip().lower()

        aliases = {
            "طلا": "XAUUSD",
            "gold": "XAUUSD",
            "xauusd": "XAUUSD",
            "eurusd": "EURUSD",
            "یورو": "EURUSD",
            "پوند": "GBPUSD",
            "gbpusd": "GBPUSD",
            "ین": "USDJPY",
            "usdjpy": "USDJPY",
            "audusd": "AUDUSD",
            "usdchf": "USDCHF",
            "usdcad": "USDCAD"
        }

        selected = None

        for alias, symbol in aliases.items():
            if alias in text_value and symbol in SYMBOLS:
                selected = symbol
                break

        if selected is None:
            bot.send_message(
                message.chat.id,
                "Please select a symbol using /start."
            )
            return

        if any(word in text_value for word in (
            "100",
            "صد",
            "تاریخچه",
            "کندل",
            "گذشته",
            "history"
        )):
            bot.send_message(
                message.chat.id,
                f"Scanning the last 100 candles of {selected}..."
            )

            report = historical_report(selected)

            bot.send_message(
                message.chat.id,
                report,
                parse_mode="HTML"
            )

        else:
            bot.send_message(
                message.chat.id,
                symbol_status(selected),
                parse_mode="HTML",
                reply_markup=build_symbol_keyboard(selected)
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


@app.route("/")
def home():
    return {
        "service": "MACD Divergence Scanner",
        "status": "running",
        "timeframe": "M1",
        "symbols": list(SYMBOLS.keys()),
        "historical_report_candles": 100,
        "timezone": "Asia/Tehran"
    }


@app.route("/health")
def health():
    return {
        "status": "ok",
        "symbols": len(SYMBOLS),
        "telegram": bool(bot)
    }


def main():
    init_db()

    threading.Thread(
        target=scanner_loop,
        daemon=True
    ).start()

    if bot:
        threading.Thread(
            target=run_telegram,
            daemon=True
        ).start()

    port = int(os.getenv("PORT", "8080"))

    app.run(
        host="0.0.0.0",
        port=port,
        debug=False,
        use_reloader=False
    )


if __name__ == "__main__":
    main()
