import os
import re
import json
import html
import time
import random
import string
import threading
from datetime import datetime
import pytz
import websocket
from flask import Flask, request
import telebot
import pandas as pd
import numpy as np

# ==================== ⚙️ تنظیمات اولیه ====================
BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN is not set in environment variables")

bot = telebot.TeleBot(BOT_TOKEN)

SYMBOLS = [
    {"symbol": "OANDA:XAUUSD", "name": "XAUUSD (طلا)"},
    {"symbol": "FX_IDC:EURUSD", "name": "EURUSD"},
    {"symbol": "FX_IDC:GBPUSD", "name": "GBPUSD"},
    {"symbol": "FX_IDC:USDJPY", "name": "USDJPY"}
]

STATE_FILE = "bot_state.json"
subscribed_users = set()
signal_history = []
last_source_name = "در حال اتصال مستقیم به TradingView..."

_state_lock = threading.Lock()

app = Flask(__name__)


def save_state():
    with _state_lock:
        try:
            with open(STATE_FILE, "w") as f:
                json.dump({"users": list(subscribed_users), "history": signal_history}, f)
        except Exception:
            pass


def load_state():
    global subscribed_users, signal_history
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r") as f:
                data = json.load(f)
                subscribed_users = set(data.get("users", []))
                signal_history = data.get("history", [])
    except Exception:
        pass


load_state()


def broadcast(text, reply_markup=None):
    if not bot:
        return
    for chat_id in list(subscribed_users):
        try:
            bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=reply_markup)
        except Exception as e:
            print(f"Telegram send error: {e}")


@app.route('/')
def home():
    return f"TradingView WebSocket MACD Bot is LIVE! Source: {last_source_name}"


def run_web():
    port = int(os.environ.get("PORT", 7860))
    app.run(host="0.0.0.0", port=port)


# ==================== 🔌 موتور اتصال وب‌سوکت تریدینگ‌ویو ====================
def _tv_send(ws, func, params):
    payload = json.dumps({"m": func, "p": params}, separators=(",", ":"))
    frame = f"~m~{len(payload)}~m~{payload}"
    ws.send(frame)


def fetch_tv_candles(symbol="OANDA:XAUUSD", interval="5", n_bars=250):
    global last_source_name
    ws = None
    try:
        ws = websocket.create_connection(
            "wss://data.tradingview.com/socket.io/websocket?from=chart%2F",
            headers={
                "Origin": "https://www.tradingview.com",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            },
            timeout=12
        )
        ws.settimeout(5)

        chart_session = "cs" + "".join(random.choices(string.ascii_lowercase + string.digits, k=12))
        _tv_send(ws, "set_auth_token", ["unauthorized_user_token"])
        _tv_send(ws, "chart_create_session", [chart_session, ""])
        sym_json = json.dumps({"symbol": symbol, "adjustment": "splits"}, separators=(",", ":"))
        _tv_send(ws, "resolve_symbol", [chart_session, "sds_sym_1", f"={sym_json}"])
        _tv_send(ws, "create_series", [chart_session, "sds_1", "s1", "sds_sym_1", str(interval), n_bars, ""])

        bars = []
        start_t = time.time()
        while time.time() - start_t < 10:
            try:
                raw = ws.recv()
            except Exception:
                break
            if not raw:
                break
            if raw.startswith("~m~") and "~h~" in raw:
                try:
                    ws.send(raw)
                except Exception:
                    pass
                continue
            parts = re.split(r"~m~\d+~m~", raw)
            for part in parts:
                if not part.strip():
                    continue
                try:
                    msg = json.loads(part)
                except Exception:
                    continue
                m_type = msg.get("m")
                if m_type in ("timescale_update", "du"):
                    p_data = msg.get("p", [])
                    if len(p_data) >= 2 and isinstance(p_data[1], dict):
                        sds = p_data[1].get("sds_1", {})
                        s_list = sds.get("s", [])
                        for item in s_list:
                            v = item.get("v", [])
                            if len(v) >= 5:
                                bars.append({
                                    "Time": int(v[0]),
                                    "Open": float(v[1]),
                                    "High": float(v[2]),
                                    "Low": float(v[3]),
                                    "Close": float(v[4])
                                })
                elif m_type == "series_completed":
                    break
            if len(bars) >= 50:
                break

        try:
            ws.close()
        except Exception:
            pass

        if len(bars) < 50:
            return pd.DataFrame()

        df = pd.DataFrame(bars)
        df["Time"] = pd.to_datetime(df["Time"], unit="s", utc=True)
        df.set_index("Time", inplace=True)
        df = df[~df.index.duplicated(keep="last")].sort_index()
        last_source_name = f"TradingView ({symbol})"
        return df
    except Exception as e:
        if ws:
            try:
                ws.close()
            except Exception:
                pass
        return pd.DataFrame()


# ==================== 📊 اندیکاتورها و تحلیل استراتژی ====================
def calculate_macd(df: pd.DataFrame, fast: int = 48, slow: int = 106, signal: int = 36) -> pd.DataFrame:
    df = df.copy()
    ema_fast = df['Close'].ewm(span=fast, adjust=False).mean()
    ema_slow = df['Close'].ewm(span=slow, adjust=False).mean()
    df['macd_line'] = ema_fast - ema_slow
    df['signal_line'] = df['macd_line'].ewm(span=signal, adjust=False).mean()
    df['histogram'] = df['macd_line'] - df['signal_line']
    return df


def find_pivots(df: pd.DataFrame, order: int = 5) -> pd.DataFrame:
    df = df.copy()
    df['pivot_high'] = np.nan
    df['pivot_low'] = np.nan
    highs = df['High'].values
    lows = df['Low'].values

    for i in range(order, len(df) - order):
        if highs[i] == max(highs[i - order:i + order + 1]):
            df.iloc[i, df.columns.get_loc('pivot_high')] = highs[i]
        if lows[i] == min(lows[i - order:i + order + 1]):
            df.iloc[i, df.columns.get_loc('pivot_low')] = lows[i]
    return df


def evaluate_multi_timeframe(sym_dict: dict) -> dict:
    symbol = sym_dict["symbol"]
    name = sym_dict["name"]

    df_daily = fetch_tv_candles(symbol, interval="D", n_bars=100)
    df_m5 = fetch_tv_candles(symbol, interval="5", n_bars=250)

    if df_daily.empty or df_m5.empty:
        return {"signal": None}

    df_daily = calculate_macd(df_daily, fast=48, slow=106, signal=36)
    daily_bias = "BULLISH" if df_daily['histogram'].iloc[-1] > 0 else "BEARISH"

    df_m5 = calculate_macd(df_m5, fast=48, slow=106, signal=36)
    df_m5 = find_pivots(df_m5, order=5)

    recent_lows = df_m5[df_m5['pivot_low'].notna()].tail(2)
    recent_highs = df_m5[df_m5['pivot_high'].notna()].tail(2)

    current_price = float(df_m5['Close'].iloc[-1])

    if len(recent_lows) == 2 and daily_bias == "BULLISH":
        p1, p2 = recent_lows.iloc[0], recent_lows.iloc[1]
        if p2['pivot_low'] < p1['pivot_low'] and p2['macd_line'] > p1['macd_line']:
            return {
                "signal": "BUY",
                "name": name,
                "price": current_price,
                "text": f"🟢 <b>دایورجنس صعودی M5 با تایید روند صعودی دیلی</b>\n💰 قیمت: <code>{current_price:.2f}</code>"
            }

    if len(recent_highs) == 2 and daily_bias == "BEARISH":
        h1, h2 = recent_highs.iloc[0], recent_highs.iloc[1]
        if h2['pivot_high'] > h1['pivot_high'] and h2['macd_line'] < h1['macd_line']:
            return {
                "signal": "SELL",
                "name": name,
                "price": current_price,
                "text": f"🔴 <b>دایورجنس نزولی M5 با تایید روند نزولی دیلی</b>\n💰 قیمت: <code>{current_price:.2f}</code>"
            }

    return {"signal": None}


# ==================== 🤖 رابط کاربری تلگرام ====================
def get_main_keyboard():
    markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add(telebot.types.KeyboardButton("📊 وضعیت ربات"), telebot.types.KeyboardButton("📜 آخرین سیگنال‌ها (History)"))
    return markup


@bot.message_handler(commands=['start'])
def handle_start(message):
    chat_id = message.chat.id
    subscribed_users.add(chat_id)
    save_state()
    bot.send_message(
        chat_id,
        "🤖 <b>ربات همزمانی مکدی (اتصال مستقیم وب‌سوکت تریدینگ‌ویو) فعال شد!</b>",
        parse_mode="HTML",
        reply_markup=get_main_keyboard()
    )


@bot.message_handler(func=lambda message: message.text == "📊 وضعیت ربات")
def handle_status(message):
    txt = (
        f"📊 <b>وضعیت ربات:</b>\n\n"
        f"📡 منبع داده: <code>{last_source_name}</code>\n"
        f"👥 کاربران فعال: <b>{len(subscribed_users)}</b>\n"
        f"🟢 سیستم وب‌سوکت بدون نیاز به API یا تنظیمات دستی روشن است."
    )
    bot.send_message(message.chat.id, txt, parse_mode="HTML", reply_markup=get_main_keyboard())


@bot.message_handler(func=lambda message: message.text == "📜 آخرین سیگنال‌ها (History)")
def handle_history(message):
    if not signal_history:
        bot.send_message(message.chat.id, "📂 هنوز سیگنالی ثبت نشده است.", reply_markup=get_main_keyboard())
    else:
        history_text = "📜 <b>آخرین سیگنال‌های تاییدشده:</b>\n\n" + "\n".join(signal_history)
        bot.send_message(message.chat.id, history_text, parse_mode="HTML", reply_markup=get_main_keyboard())


# ==================== 🔄 اسکنر پس‌زمینه ====================
def background_scanner():
    global signal_history
    sent_tracker = {}
    time.sleep(15)

    while True:
        try:
            if not subscribed_users:
                time.sleep(10)
                continue

            for sym_dict in SYMBOLS:
                symbol = sym_dict["symbol"]
                res = evaluate_multi_timeframe(sym_dict)
                time.sleep(2)

                if res.get("signal"):
                    sig_type = res["signal"]
                    key = f"{symbol}_{sig_type}"

                    if sent_tracker.get(key) != "sent":
                        sent_tracker[key] = "sent"
                        msg = (
                            f"🚨 <b>سیگنال همزمانی MACD (TradingView WebSocket)</b> 🚨\n\n"
                            f"🔹 نماد: <b>{res['name']}</b>\n{res['text']}"
                        )

                        signal_history.insert(0, f"🔹 {res['name']} | {sig_type} | قیمت: {res['price']}")
                        if len(signal_history) > 10:
                            signal_history.pop()
                        save_state()

                        broadcast(msg, reply_markup=get_main_keyboard())
                else:
                    sent_tracker.pop(f"{symbol}_BUY", None)
                    sent_tracker.pop(f"{symbol}_SELL", None)

        except Exception as e:
            print(f"Scanner error: {e}")

        time.sleep(120)


# ==================== 🚀 اجرای همزمان سرویس‌ها ====================
if __name__ == "__main__":
    threading.Thread(target=run_web, daemon=True).start()
    threading.Thread(target=background_scanner, daemon=True).start()

    t_bot = threading.Thread(
        target=bot.infinity_polling,
        kwargs={"skip_pending": True},
        daemon=True,
    )
    t_bot.start()

    print("Bot is running with direct TradingView WebSockets...")
    while True:
        time.sleep(60)
