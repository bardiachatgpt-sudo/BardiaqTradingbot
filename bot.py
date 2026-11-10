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

# ۸ نماد اصلی
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
subscribed_users = set()
last_source_name = "در حال اتصال به TradingView..."

# ساختار برای نگهداری سیگنال‌های فعال (گزینه B)
active_signals = {}
state_lock = threading.Lock()

app = Flask(__name__)

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

def broadcast(text):
    for chat_id in list(subscribed_users):
        try:
            bot.send_message(chat_id, text, parse_mode="HTML")
        except Exception as e:
            print(f"Telegram error: {e}")

# ==================== 🌐 وب‌سرور ====================
@app.route('/')
def home():
    return f"Divergence Bot is LIVE! Source: {last_source_name}"

def run_web():
    port = int(os.environ.get("PORT", 7860))
    app.run(host="0.0.0.0", port=port)

# ==================== 🔌 اتصال به TradingView ====================
def _tv_send(ws, func, params):
    payload = json.dumps({"m": func, "p": params}, separators=(",", ":"))
    frame = f"~m~{len(payload)}~m~{payload}"
    ws.send(frame)

# n_bars رو به عنوان ورودی اضافه کردیم تا بتونیم برای دکمه وضعیت بازار، سریع‌تر دیتا بگیریم
def fetch_tv_candles(symbol, interval, n_bars=300):
    global last_source_name
    ws = None
    try:
        ws = websocket.create_connection(
            "wss://data.tradingview.com/socket.io/websocket?from=chart%2F",
            headers={
                "Origin": "https://www.tradingview.com",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
            },
            timeout=15
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
            try:
                raw = ws.recv()
            except Exception:
                break
            if not raw:
                break
            if raw.startswith("~m~") and "~h~" in raw:
                try: ws.send(raw)
                except: pass
                continue
                
            parts = re.split(r"~m~\d+~m~", raw)
            for part in parts:
                if not part.strip(): continue
                try: msg = json.loads(part)
                except: continue
                
                m_type = msg.get("m")
                if m_type in ("timescale_update", "du"):
                    p_data = msg.get("p", [])
                    if len(p_data) >= 2 and isinstance(p_data[1], dict):
                        sds = p_data[1].get("sds_1", {})
                        for item in sds.get("s", []):
                            v = item.get("v", [])
                            if len(v) >= 5:
                                bars.append({
                                    "Time": int(v[0]), "Open": float(v[1]),
                                    "High": float(v[2]), "Low": float(v[3]), "Close": float(v[4])
                                })
                elif m_type == "series_completed":
                    break
            if len(bars) >= 50:
                break

        try: ws.close()
        except: pass

        if len(bars) < 50:
            return pd.DataFrame()

        df = pd.DataFrame(bars)
        df["Time"] = pd.to_datetime(df["Time"], unit="s", utc=True)
        df.set_index("Time", inplace=True)
        df = df[~df.index.duplicated(keep="last")].sort_index()
        last_source_name = f"TradingView ({symbol})"
        return df
    except Exception:
        if ws:
            try: ws.close()
            except: pass
        return pd.DataFrame()

# ==================== 📊 محاسبات MACD و پیوت ====================
def calculate_macd(df):
    df = df.copy()
    # تنظیمات عین عکس: 48, 104, 36
    ema_fast = df['Close'].ewm(span=48, adjust=False).mean()
    ema_slow = df['Close'].ewm(span=104, adjust=False).mean()
    df['macd'] = ema_fast - ema_slow
    df['signal'] = df['macd'].ewm(span=36, adjust=False).mean()
    df['hist'] = df['macd'] - df['signal']
    return df

def find_pivots(df, order=5):
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

# ==================== 🎯 تشخیص دایورجنس ====================
def check_divergence(symbol, interval_name, df):
    if len(df) < 50:
        return

    df = calculate_macd(df)
    df = find_pivots(df, order=5)
    
    # کندل زنده (کندل آخر کامل نیست، پس تحلیل روی کندل قبلی انجام میشه)
    df_closed = df.iloc[:-1]
    current_time = time.time()
    
    # پیدا کردن ۲ پیوت آخر (Lows برای خرید، Highs برای فروش)
    recent_lows = df_closed[df_closed['pivot_low'].notna()].tail(2)
    recent_highs = df_closed[df_closed['pivot_high'].notna()].tail(2)

    # ---------------------------------------------------------
    # ۱. بررسی دایورجنس صعودی (Bullish / BUY)
    # ---------------------------------------------------------
    if len(recent_lows) == 2:
        p1, p2 = recent_lows.iloc[0], recent_lows.iloc[1]
        
        # شرط دایورجنس: قیمت کف پایین‌تر (LL) ولی مکدی کف بالاتر (HL)
        if p2['pivot_low'] < p1['pivot_low'] and p2['macd'] > p1['macd']:
            
            # شرط گزینه B: مکدی هنوز زیر صفر باشه
            if p2['macd'] < 0:
                key = (symbol, interval_name, "BUY")
                
                # اگه سیگنال جدید بود یا ۵ دقیقه از آخرین پیام گذشته بود
                if key not in active_signals or (current_time - active_signals[key]['last_alert']) >= 300:
                    
                    active_signals[key] = {
                        'last_alert': current_time,
                        'p1_macd': p1['macd'],
                        'p2_macd': p2['macd']
                    }
                    
                    msg = (
                        f"🟢 <b>دایورجنس صعودی کلاسیک (BUY)</b>\n"
                        f"📊 نماد: <b>{symbol}</b>\n"
                        f"⏱ تایم‌فریم: <b>{interval_name}</b>\n"
                        f"━━━━━━━━━━━━━━━━━\n"
                        f"💰 قیمت فعلی: <code>{df_closed['Close'].iloc[-1]:.5f}</code>\n"
                        f"📉 MACD هنوز منفی است (تایید ورود): <code>{p2['macd']:.5f}</code>\n"
                        f"🔄 منتظر تغییر فاز به مثبت باشید.\n"
                        f"⏳ <i>هر ۵ دقیقه تا زمانی که MACD زیر صفر باشد یادآوری می‌شود...</i>"
                    )
                    broadcast(msg)

    # ---------------------------------------------------------
    # ۲. بررسی دایورجنس نزولی (Bearish / SELL)
    # ---------------------------------------------------------
    if len(recent_highs) == 2:
        h1, h2 = recent_highs.iloc[0], recent_highs.iloc[1]
        
        # شرط دایورجنس: قیمت سقف بالاتر (HH) ولی مکدی سقف پایین‌تر (LH)
        if h2['pivot_high'] > h1['pivot_high'] and h2['macd'] < h1['macd']:
            
            # شرط گزینه B: مکدی هنوز بالای صفر باشه
            if h2['macd'] > 0:
                key = (symbol, interval_name, "SELL")
                
                if key not in active_signals or (current_time - active_signals[key]['last_alert']) >= 300:
                    
                    active_signals[key] = {
                        'last_alert': current_time,
                        'h1_macd': h1['macd'],
                        'h2_macd': h2['macd']
                    }
                    
                    msg = (
                        f"🔴 <b>دایورجنس نزولی کلاسیک (SELL)</b>\n"
                        f"📊 نماد: <b>{symbol}</b>\n"
                        f"⏱ تایم‌فریم: <b>{interval_name}</b>\n"
                        f"━━━━━━━━━━━━━━━━━\n"
                        f"💰 قیمت فعلی: <code>{df_closed['Close'].iloc[-1]:.5f}</code>\n"
                        f"📈 MACD هنوز مثبت است (تایید ورود): <code>{h2['macd']:.5f}</code>\n"
                        f"🔄 منتظر تغییر فاز به منفی باشید.\n"
                        f"⏳ <i>هر ۵ دقیقه تا زمانی که MACD بالای صفر باشد یادآوری می‌شود...</i>"
                    )
                    broadcast(msg)

    # ---------------------------------------------------------
    # ۳. پاکسازی سیگنال‌های منقضی شده (وقتی MACD از صفر رد شد)
    # ---------------------------------------------------------
    current_macd = df_closed['macd'].iloc[-1]
    for key in list(active_signals.keys()):
        _, _, sig_type = key
        if sig_type == "BUY" and current_macd >= 0:
            del active_signals[key]  # مکدی مثبت شد، سیگنال خرید سوخت
        elif sig_type == "SELL" and current_macd <= 0:
            del active_signals[key]  # مکدی منفی شد، سیگنال فروش سوخت

# ==================== 🔄 حلقه اسکنر ====================
def scanner_loop():
    time.sleep(10) # مکث برای بالا اومدن وب‌سرور
    
    while True:
        try:
            if not subscribed_users:
                time.sleep(10)
                continue

            for sym in SYMBOLS:
                # بررسی تایم‌فریم ۵ دقیقه
                df_m5 = fetch_tv_candles(sym["symbol"], "5", 300)
                if not df_m5.empty:
                    check_divergence(sym["name"], "M5", df_m5)
                
                time.sleep(1) # وقفه کوتاه
                
                # بررسی تایم‌فریم ۱ دقیقه
                df_m1 = fetch_tv_candles(sym["symbol"], "1", 300)
                if not df_m1.empty:
                    check_divergence(sym["name"], "M1", df_m1)
                
                time.sleep(2) # وقفه بین نمادها برای جلوگیری از بلاک شدن
                
        except Exception as e:
            print(f"Scanner error: {e}")
            
        time.sleep(60) # اسکن کلی هر ۱ دقیقه (چون تایم‌فریم ۱ دقیقه داریم)

# ==================== 🤖 تلگرام ====================
def get_keyboard():
    markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    # دکمه جدید وضعیت بازار اضافه شد
    markup.add("📊 وضعیت ربات", "📰 وضعیت بازار")
    return markup

@bot.message_handler(commands=['start'])
def cmd_start(message):
    subscribed_users.add(message.chat.id)
    save_state()
    bot.send_message(
        message.chat.id, 
        "🤖 <b>ربات دایورجنس کلاسیک MACD (48, 104, 36)</b>\n\n"
        "سیستم فعال شد. دایورجنس‌های معتبر با تایید MACD (گزینه B) ارسال می‌شوند.",
        parse_mode="HTML", reply_markup=get_keyboard()
    )

@bot.message_handler(func=lambda m: m.text == "📊 وضعیت ربات")
def cmd_status(message):
    txt = (
        f"📊 <b>وضعیت ربات:</b>\n\n"
        f"🔗 منبع: <code>{last_source_name}</code>\n"
        f"👥 کاربران: <b>{len(subscribed_users)}</b>\n"
        f"🎯 سیگنال‌های فعال در انتظار تایید: <b>{len(active_signals)}</b>\n"
        f"🟢 سیستم آنلاین است."
    )
    bot.send_message(message.chat.id, txt, parse_mode="HTML", reply_markup=get_keyboard())

# ==================== 📰 دکمه وضعیت بازار ====================
@bot.message_handler(func=lambda m: m.text == "📰 وضعیت بازار")
def cmd_market_status(message):
    msg = bot.send_message(message.chat.id, "⏳ در حال دریافت قیمت‌های لحظه‌ای بازار...")
    
    market_text = "📰 <b>وضعیت لحظه‌ای بازار (M5)</b>\n━━━━━━━━━━━━━━━━━\n"
    
    for sym in SYMBOLS:
        # برای اینکه سریع باشه، فقط ۵۰ کندل آخر M5 رو می‌گیریم
        df = fetch_tv_candles(sym["symbol"], "5", 50)
        
        if not df.empty:
            df = calculate_macd(df)
            current_price = df['Close'].iloc[-1]
            current_macd = df['macd'].iloc[-1]
            
            # تشخیص فاز مکدی
            if current_macd > 0:
                macd_status = "مثبت (صعودی) 🟢"
            else:
                macd_status = "منفی (نزولی) 🔴"
                
            market_text += (
                f"🔹 <b>{sym['name']}</b>\n"
                f"💰 قیمت: <code>{current_price:.5f}</code>\n"
                f"📊 MACD: {macd_status} (<code>{current_macd:.5f}</code>)\n"
                f"───────────────\n"
            )
        else:
            market_text += f"🔹 <b>{sym['name']}</b>\n❌ خطا در دریافت داده\n───────────────\n"
        
        time.sleep(1) # وقفه کوتاه بین درخواست‌ها
    
    bot.edit_message_text(
        market_text, 
        chat_id=message.chat.id, 
        message_id=msg.message_id, 
        parse_mode="HTML"
    )

# ==================== 🚀 اجرا ====================
if __name__ == "__main__":
    threading.Thread(target=run_web, daemon=True).start()
    threading.Thread(target=scanner_loop, daemon=True).start()
    
    t_bot = threading.Thread(
        target=bot.infinity_polling,
        kwargs={"skip_pending": True},
        daemon=True
    )
    t_bot.start()
    
    print("Bot is running...")
    while True:
        time.sleep(60)
