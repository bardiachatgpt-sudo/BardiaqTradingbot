import os
import re
import json
import time
import random
import string
import threading
import websocket
import telebot
import pandas as pd
import numpy as np
from flask import Flask
from datetime import datetime

# ==================== ⚙️ تنظیمات ====================
BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN is not set")

bot = telebot.TeleBot(BOT_TOKEN)

SYMBOLS = [
    {"symbol": "OANDA:XAUUSD", "name": "XAUUSD (طلا)"},
    {"symbol": "FX_IDC:EURUSD", "name": "EURUSD"},
    {"symbol": "FX_IDC:GBPUSD", "name": "GBPUSD"},
    {"symbol": "FX_IDC:USDJPY", "name": "USDJPY"},
    {"symbol": "FX_IDC:AUDUSD", "name": "AUDUSD"},
    {"symbol": "FX_IDC:USDCAD", "name": "USDCAD"},
    {"symbol": "FX_IDC:NZDUSD", "name": "NZDUSD"},
    {"symbol": "FX_IDC:USDCHF", "name": "USDCHF"}
]

STATE_FILE = "bot_state.json"
SIGNALS_FILE = "signals_log.json"

subscribed_users = set()
state_lock = threading.Lock()
app = Flask(__name__)

active_trades = {}

# ==================== 💾 ذخیره و بارگذاری ====================
def save_state():
    with state_lock:
        try:
            with open(STATE_FILE, "w") as f:
                json.dump({"users": list(subscribed_users)}, f)
        except Exception:
            pass

def load_state():
    global subscribed_users
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r") as f:
                data = json.load(f)
                subscribed_users = set(data.get("users", []))
    except Exception:
        pass

load_state()

def save_signal(sig):
    try:
        data = []
        if os.path.exists(SIGNALS_FILE):
            with open(SIGNALS_FILE, "r") as f:
                data = json.load(f)
        data.insert(0, {**sig, "time": time.time()})
        data = data[:50]
        with open(SIGNALS_FILE, "w") as f:
            json.dump(data, f)
    except Exception:
        pass

# ==================== 🌐 وب‌سرور ====================
@app.route('/')
def home():
    return "3-MACD Full Strategy Bot is LIVE!"

def run_web():
    port = int(os.environ.get("PORT", 7860))
    app.run(host="0.0.0.0", port=port)

# ==================== 🔌 اتصال به TradingView ====================
def _tv_send(ws, func, params):
    payload = json.dumps({"m": func, "p": params}, separators=(",", ":"))
    frame = f"~m~{len(payload)}~m~{payload}"
    ws.send(frame)

def fetch_tv_candles(symbol, interval, n_bars=200):
    ws = None
    try:
        ws = websocket.create_connection(
            "wss://data.tradingview.com/socket.io/websocket?from=chart%2F",
            headers={"Origin": "https://www.tradingview.com", "User-Agent": "Mozilla/5.0"},
            timeout=12
        )
        ws.settimeout(5)
        session = "cs" + "".join(random.choices(string.ascii_lowercase + string.digits, k=12))
        _tv_send(ws, "set_auth_token", ["unauthorized_user_token"])
        _tv_send(ws, "chart_create_session", [session, ""])
        
        sym_json = json.dumps({"symbol": symbol, "adjustment": "splits"}, separators=(",", ":"))
        _tv_send(ws, "resolve_symbol", [session, "sds_sym_1", f"={sym_json}"])
        _tv_send(ws, "create_series", [session, "sds_1", "s1", "sds_sym_1", str(interval), n_bars, ""])
        
        bars = []
        start_t = time.time()
        while time.time() - start_t < 10:
            try: raw = ws.recv()
            except: break
            if not raw: break
            if raw.startswith("~m~") and "~h~" in raw:
                try: ws.send(raw)
                except: pass
                continue
            
            parts = re.split(r"~m~\d+~m~", raw)
            for part in parts:
                if not part.strip(): continue
                try: msg = json.loads(part)
                except: continue
                
                if msg.get("m") in ("timescale_update", "du"):
                    p_data = msg.get("p", [])
                    if len(p_data) >= 2 and isinstance(p_data[1], dict):
                        for item in p_data[1].get("sds_1", {}).get("s", []):
                            v = item.get("v", [])
                            if len(v) >= 5:
                                bars.append({"Time": int(v[0]), "Open": float(v[1]), "High": float(v[2]), "Low": float(v[3]), "Close": float(v[4])})
                elif msg.get("m") == "series_completed":
                    break
            if len(bars) >= 50: break
        
        try: ws.close()
        except: pass

        if len(bars) < 50: return pd.DataFrame()

        df = pd.DataFrame(bars)
        df["Time"] = pd.to_datetime(df["Time"], unit="s", utc=True)
        df.set_index("Time", inplace=True)
        return df[~df.index.duplicated(keep="last")].sort_index()
    except Exception:
        if ws:
            try: ws.close()
            except: pass
        return pd.DataFrame()

# ==================== 📊 محاسبات مکدی و پیوت ====================
def add_macd(df, fast, slow, signal, col_prefix):
    df = df.copy()
    ema_fast = df['Close'].ewm(span=fast, adjust=False).mean()
    ema_slow = df['Close'].ewm(span=slow, adjust=False).mean()
    df[f'{col_prefix}_macd'] = ema_fast - ema_slow
    df[f'{col_prefix}_signal'] = df[f'{col_prefix}_macd'].ewm(span=signal, adjust=False).mean()
    df[f'{col_prefix}_hist'] = df[f'{col_prefix}_macd'] - df[f'{col_prefix}_signal']
    return df

def find_pivots(df, col_name, order=5):
    df = df.copy()
    df[f'{col_name}_pivot_low'] = np.nan
    df[f'{col_name}_pivot_high'] = np.nan
    vals = df[col_name].values
    
    for i in range(order, len(df) - order):
        if vals[i] == min(vals[i - order:i + order + 1]):
            df.iloc[i, df.columns.get_loc(f'{col_name}_pivot_low')] = vals[i]
        if vals[i] == max(vals[i - order:i + order + 1]):
            df.iloc[i, df.columns.get_loc(f'{col_name}_pivot_high')] = vals[i]
    return df

# ==================== 🎯 منطق استراتژی ====================
def check_step1_daily(df_daily):
    """گام ۱: بررسی مکدی کوچیک دیلی"""
    if len(df_daily) < 20: return False
    
    df = add_macd(df_daily, 3, 6, 2, 'small')
    df_closed = df.iloc[:-1]
    
    current_macd = df_closed['small_macd'].iloc[-1]
    hist = df_closed['small_hist'].iloc[-1]
    
    return current_macd < 0 or hist < 0

def check_step2_h4_divergence(df_h4):
    """گام ۲: بررسی دایورجنس در تایم ۴ ساعته با مکدی دیفالت"""
    if len(df_h4) < 50: return None
    
    df = add_macd(df_h4, 12, 26, 9, 'def')
    df = find_pivots(df, 'def_macd', order=5)
    df_closed = df.iloc[:-1]

    # دایورجنس صعودی
    lows = df_closed[df_closed['def_macd_pivot_low'].notna()].tail(2)
    if len(lows) == 2:
        p1, p2 = lows.iloc[0], lows.iloc[1]
        if p2['Low'] < p1['Low'] and p2['def_macd'] > p1['def_macd']:
            return "BUY"
            
    # دایورجنس نزولی
    highs = df_closed[df_closed['def_macd_pivot_high'].notna()].tail(2)
    if len(highs) == 2:
        h1, h2 = highs.iloc[0], highs.iloc[1]
        if h2['High'] > h1['High'] and h2['def_macd'] < h1['def_macd']:
            return "SELL"
            
    return None

def check_step3_m1_entry(df_m1, direction):
    """گام ۳: تغییر فاز مکدی دیفالت در M1"""
    if len(df_m1) < 20: return None
    
    df = add_macd(df_m1, 12, 26, 9, 'def')
    df_closed = df.iloc[:-1]
    
    last = df_closed.iloc[-1]
    prev = df_closed.iloc[-2]
    current_price = last['Close']

    if direction == "BUY":
        if prev['def_macd'] < 0 and last['def_macd'] > 0:
            return {"type": "BUY", "price": current_price}

    elif direction == "SELL":
        if prev['def_macd'] > 0 and last['def_macd'] < 0:
            return {"type": "SELL", "price": current_price}

    return None

# ==================== 🔄 اسکنر پس‌زمینه ====================
def scanner_loop():
    time.sleep(10)
    
    while True:
        try:
            if not subscribed_users:
                time.sleep(10)
                continue

            for sym in SYMBOLS:
                sym_name = sym["name"]
                sym_code = sym["symbol"]
                
                if sym_name in active_trades:
                    continue

                # گام ۱: دیلی
                df_daily = fetch_tv_candles(sym_code, "1D", 50)
                if df_daily.empty or not check_step1_daily(df_daily):
                    time.sleep(2)
                    continue
                
                # گام ۲: ۴ ساعته
                df_h4 = fetch_tv_candles(sym_code, "240", 100)
                if df_h4.empty:
                    time.sleep(2)
                    continue
                
                trend = check_step2_h4_divergence(df_h4)
                if not trend:
                    time.sleep(2)
                    continue
                
                # گام ۳: ۱ دقیقه
                df_m1 = fetch_tv_candles(sym_code, "1", 50)
                if df_m1.empty:
                    time.sleep(2)
                    continue
                
                setup = check_step3_m1_entry(df_m1, trend)
                
                if setup:
                    active_trades[sym_name] = setup
                    save_signal({"symbol": sym_name, "type": setup['type'], "price": setup['price']})
                    
                    msg = (
                        f"🚨 <b>سیگنال استراتژی ۳ مکدی</b> 🚨\n\n"
                        f"🔹 نماد: <b>{sym_name}</b>\n"
                        f"📉 جهت: <b>{setup['type']}</b>\n"
                        f"⏱ تریگر: تغییر فاز مکدی دیفالت M1\n"
                        f"💰 قیمت ورود: <code>{setup['price']:.5f}</code>\n\n"
                        f"✅ <i>تایید شده توسط: مکدی کوچیک دیلی + دایورجنس ۴ ساعته</i>"
                    )
                    
                    for chat_id in list(subscribed_users):
                        try:
                            bot.send_message(chat_id, msg, parse_mode="HTML")
                        except Exception as e:
                            print(f"Send error: {e}")
                            
                    threading.Timer(300, lambda s=sym_name: active_trades.pop(s, None)).start()

                time.sleep(2)

        except Exception as e:
            print(f"Scanner error: {e}")
            
        time.sleep(60)

# ==================== 🤖 تلگرام ====================
def get_keyboard():
    markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add(
        telebot.types.KeyboardButton("📊 وضعیت ربات"),
        telebot.types.KeyboardButton("📜 تاریخچه سیگنال‌ها")
    )
    return markup

@bot.message_handler(commands=['start'])
def cmd_start(message):
    with state_lock:
        subscribed_users.add(message.chat.id)
    save_state()
    
    # پاک کردن دکمه‌های قدیمی
    bot.send_message(
        message.chat.id,
        "♻️ در حال بروزرسانی منو...",
        reply_markup=telebot.types.ReplyKeyboardRemove()
    )
    
    # ارسال دکمه‌های جدید
    bot.send_message(
        message.chat.id, 
        "🤖 <b>ربات استراتژی کامل ۳ مکدی فعال شد!</b>\n"
        "روند: دیلی (کوچیک) ➔ ۴ ساعته (دایورجنس) ➔ ۱ دقیقه (تغییر فاز)",
        parse_mode="HTML", 
        reply_markup=get_keyboard()
    )

@bot.message_handler(func=lambda m: m.text == "📊 وضعیت ربات")
def cmd_status(message):
    txt = (
        f"📊 <b>وضعیت ربات:</b>\n\n"
        f"👥 کاربران: <b>{len(subscribed_users)}</b>\n"
        f"🎯 در حال انتظار: <b>{len(active_trades)}</b>\n"
        f"🟢 آنلاین."
    )
    bot.send_message(message.chat.id, txt, parse_mode="HTML", reply_markup=get_keyboard())

@bot.message_handler(func=lambda m: m.text == "📜 تاریخچه سیگنال‌ها")
def cmd_history(message):
    if not os.path.exists(SIGNALS_FILE):
        bot.send_message(message.chat.id, "📂 خالی است.")
        return
    with open(SIGNALS_FILE, "r") as f:
        data = json.load(f)
    if not data:
        bot.send_message(message.chat.id, "📂 خالی است.")
        return
    text = "📜 <b>آخرین سیگنال‌ها:</b>\n\n"
    for s in data[:10]:
        t = datetime.fromtimestamp(s["time"]).strftime("%H:%M")
        text += f"🕐 {t} | {s['symbol']} | {s['type']} | {s['price']:.5f}\n"
    bot.send_message(message.chat.id, text, parse_mode="HTML")

# ==================== 🚀 اجرا ====================
if __name__ == "__main__":
    threading.Thread(target=run_web, daemon=True).start()
    threading.Thread(target=scanner_loop, daemon=True).start()
    
    threading.Thread(
        target=bot.infinity_polling,
        kwargs={"skip_pending": True},
        daemon=True
    ).start()
    
    print("Bot is running...")
    while True:
        time.sleep(60)
