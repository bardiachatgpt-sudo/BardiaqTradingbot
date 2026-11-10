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
from telebot import types
import pandas as pd
import numpy as np

# ==========================================
# ۱. تنظیمات اولیه و وب‌سرور برای Render
# ==========================================
app = Flask(__name__)
last_source_name = "در حال اتصال..."

BOT_TOKEN = os.environ.get("BOT_TOKEN")
bot = telebot.TeleBot(BOT_TOKEN) if BOT_TOKEN else None

STATE_FILE = "bot_state.json"
subscribed_users = set()
last_divergences = {}  # ذخیره آخرین سیگنال هر نماد

# تنظیم منطقه زمانی تهران
tehran_tz = pytz.timezone('Asia/Tehran')

def save_state():
    try:
        with open(STATE_FILE, "w") as f:
            serializable_divs = {}
            for sym, data in last_divergences.items():
                serializable_divs[sym] = {
                    "type": data["type"],
                    "price": data["price"],
                    "macd": data["macd"],
                    "time": str(data["time"]) 
                }
            json.dump({"users": list(subscribed_users), "last_divs": serializable_divs}, f)
    except Exception as e:
        print(f"Save state error: {e}")

def load_state():
    global subscribed_users, last_divergences
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r") as f:
                data = json.load(f)
                subscribed_users = set(data.get("users", []))
                last_divergences = data.get("last_divs", {})
    except Exception as e:
        print(f"Load state error: {e}")

load_state()

def broadcast(text):
    if not bot: return
    for chat_id in list(subscribed_users):
        try:
            bot.send_message(chat_id, text, parse_mode="HTML")
        except Exception as e:
            print(f"Telegram send error: {e}")

# ==========================================
# ۲. هندلرهای ربات تلگرام (دکمه‌ها و دستورات)
# ==========================================
def get_main_keyboard():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    btn_last = types.KeyboardButton("📊 آخرین سیگنال‌ها")
    markup.add(btn_last)
    return markup

@bot.message_handler(commands=['start'])
def send_welcome(message):
    subscribed_users.add(message.chat.id)
    save_state()
    bot.reply_to(
        message, 
        "✅ ربات اسکنر MACD (نسخه مولتی‌نماد) روشن شد.\n\n"
        "استراتژی: دایورجنس ۳ صفر (تایم ۱ دقیقه)\n"
        "نمادها: طلا و جفت‌ارزهای ماژور\n\n"
        "از دکمه زیر می‌تونید آخرین وضعیت سیگنال‌ها رو ببینید:", 
        reply_markup=get_main_keyboard()
    )

@bot.message_handler(func=lambda message: message.text == '📊 آخرین سیگنال‌ها' or message.text == '/last')
def send_last_signals(message):
    if not last_divergences:
        bot.reply_to(message, "⏳ هنوز هیچ سیگنالی در این نشست ثبت نشده است.")
        return

    msg = "📋 <b>آخرین دایورجنس‌های شناسایی شده:</b>\n\n"
    
    try:
        sorted_divs = sorted(last_divergences.items(), key=lambda x: pd.to_datetime(x[1]['time']), reverse=True)
    except:
        sorted_divs = list(last_divergences.items())
    
    for symbol, data in sorted_divs:
        try:
            dt_utc = pd.to_datetime(data['time'], utc=True)
            dt_tehran = dt_utc.astimezone(tehran_tz).strftime('%Y-%m-%d %H:%M')
        except:
            dt_tehran = "نامشخص"
            
        msg += (
            f"💎 <b>{symbol}</b>\n"
            f"⏰ زمان: {dt_tehran} (تهران)\n"
            f"💰 قیمت: {data['price']}\n"
            f"📉 MACD: {data['macd']}\n\n"
        )
    
    bot.reply_to(message, msg, parse_mode="HTML")

@app.route('/')
def home():
    return "MACD Divergence Multi-Symbol Bot is LIVE!"

def run_web():
    port = int(os.environ.get("PORT", 7860))
    app.run(host="0.0.0.0", port=port)

# ==========================================
# ۳. موتور اتصال به وب‌سوکت تریدینگ‌ویو
# ==========================================
def tv_send(ws, func, params):
    payload = json.dumps({"m": func, "p": params}, separators=(",", ":"))
    frame = f"~m~{len(payload)}~m~{payload}"
    ws.send(frame)

def fetch_tradingview_candles(symbol="OANDA:XAUUSD", interval="1", n_bars=220):
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
        chart_session = "cs" + "".join(random.choices(string.ascii_lowercase + string.digits, k=12))
        tv_send(ws, "set_auth_token", ["unauthorized_user_token"])
        tv_send(ws, "chart_create_session", [chart_session, ""])
        sym_json = json.dumps({"symbol": symbol, "adjustment": "splits"}, separators=(",", ":"))
        tv_send(ws, "resolve_symbol", [chart_session, "sds_sym_1", f"={sym_json}"])
        tv_send(ws, "create_series", [chart_session, "sds_1", "s1", "sds_sym_1", str(interval), n_bars, ""])
        
        bars = []
        start_t = time.time()
        while time.time() - start_t < 10:
            raw = ws.recv()
            if not raw: break
            if raw.startswith("~m~") and "~h~" in raw:
                ws.send(raw)
                continue
            parts = re.split(r"~m~\d+~m~", raw)
            for part in parts:
                if not part.strip(): continue
                try: msg = json.loads(part)
                except Exception: continue
                
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
            if len(bars) >= 50: break
        ws.close()
        
        if len(bars) < 150: 
            return None, f"bars={len(bars)}"
            
        df = pd.DataFrame(bars)
        df["Time"] = pd.to_datetime(df["Time"], unit="s", utc=True)
        df.set_index("Time", inplace=True)
        df = df[~df.index.duplicated(keep="last")].sort_index()
        return df, None
    except Exception as e:
        if ws:
            try: ws.close()
            except Exception: pass
        return None, str(e)[:50]

# ==========================================
# ۴. منطق استراتژی دایورجنس (۳ صفر و MACD)
# ==========================================
def detect_macd_divergence(df):
    if df is None or len(df) < 10:
        return None

    # تنظیمات مکدی 48, 106, 36
    exp1 = df['Close'].ewm(span=48, adjust=False).mean()
    exp2 = df['Close'].ewm(span=106, adjust=False).mean()
    df['MACD'] = exp1 - exp2
    df['Slope'] = df['MACD'].diff()

    state = 0
    p1_price, p1_macd, p1_idx = None, None, None
    signals = []

    for i in range(2, len(df)):
        current_slope = df['Slope'].iloc[i]
        prev_slope = df['Slope'].iloc[i-1]
        
        # دایورجنس صعودی (Bullish)
        if state == 0:
            # کف اول: شیب از منفی به مثبت تغییر کرده (صفر شده)
            if prev_slope < 0 and current_slope >= 0:
                if df['MACD'].iloc[i] < 0: # شرط فاز منفی
                    p1_price = df['Close'].iloc[i]
                    p1_macd = df['MACD'].iloc[i]
                    p1_idx = i
                    state = 1

        elif state == 1:
            # سقف میانی: شیب از مثبت به منفی تغییر کرده
            if prev_slope > 0 and current_slope <= 0:
                if df['MACD'].iloc[i] < 0: # کل مکدی باید زیر صفر بمونه
                    state = 2
                else:
                    state = 0 # اگر از صفر عبور کرد باطل میشه

        elif state == 2:
            # کف دوم: شیب دوباره منفی به مثبت شده
            if prev_slope < 0 and current_slope >= 0:
                if df['MACD'].iloc[i] < 0:
                    p2_price = df['Close'].iloc[i]
                    p2_macd = df['MACD'].iloc[i]
                    
                    # چک کردن اینکه کل مکدی بین کف اول و دوم زیر صفر بوده
                    all_below_zero = (df['MACD'].iloc[p1_idx:i] < 0).all()
                    
                    if all_below_zero:
                        # شرط قدر مطلق: کف اول بزرگتر از کف دوم (عمیق‌تر)
                        cond_macd = abs(p1_macd) > abs(p2_macd)
                        # شرط قیمت: قیمت کف اول بالاتر از کف دوم
                        cond_price = p1_price > p2_price
                        
                        if cond_macd and cond_price:
                            signals.append({
                                "type": "Bullish", 
                                "price": p2_price, 
                                "macd": p2_macd,
                                "time": df.index[i]
                            })
                    state = 0
    return signals

# ==========================================
# ۵. لیست نمادها و حلقه اسکنر
# ==========================================
SYMBOLS = {
    "OANDA:XAUUSD": "طلا (XAUUSD)",
    "FX:EURUSD": "یورو/دلار (EURUSD)",
    "FX:GBPUSD": "پوند/دلار (GBPUSD)",
    "FX:USDJPY": "دلار/ین (USDJPY)",
    "FX:AUDUSD": "دلار استرالیا/دلار (AUDUSD)",
    "FX:USDCAD": "دلار آمریکا/دلار کانادا (USDCAD)",
    "FX:USDCHF": "دلار آمریکا/فرانک (USDCHF)",
    "FX:NZDUSD": "دلار نیوزیلند/دلار (NZDUSD)"
}

last_signal_times = {}

def macd_scanner():
    global last_source_name, last_divergences
    while True:
        try:
            if not subscribed_users:
                time.sleep(10)
                continue
                
            for symbol, persian_name in SYMBOLS.items():
                df, err = fetch_tradingview_candles(symbol, "1", 220)
                
                if df is None:
                    print(f"Error fetching {symbol}: {err}")
                    continue
                
                last_source_name = symbol
                signals = detect_macd_divergence(df)
                
                if signals:
                    latest_sig = signals[-1]
                    sig_time = latest_sig["time"]
                    
                    # جلوگیری از ارسال سیگنال تکراری برای یک کندل
                    if last_signal_times.get(symbol) != sig_time:
                        last_signal_times[symbol] = sig_time
                        
                        # ذخیره اطلاعات برای دکمه آخرین سیگنال‌ها
                        last_divergences[symbol] = latest_sig
                        save_state()
                        
                        # تبدیل زمان به وقت تهران
                        dt_tehran = sig_time.astimezone(tehran_tz).strftime('%H:%M:%S')
                        
                        msg = (
                            f"🚀 <b>دایورجنس صعودی MACD (تایم ۱ دقیقه)</b> 🚀\n"
                            f"💎 نماد: <b>{persian_name}</b>\n\n"
                            f"📡 چارت: <code>{symbol}</code>\n"
                            f"💰 قیمت سیگنال: <code>{latest_sig['price']:.5f}</code>\n"
                            f"📉 مقدار MACD: <code>{latest_sig['macd']:.4f}</code>\n"
                            f"⏰ زمان (تهران): {dt_tehran}\n\n"
                            f"📌 <i>طبق استراتژی: کف دوم مکدی ضعیف‌تر، اما قیمت پایین‌تر.</i>"
                        )
                        broadcast(msg)
                        print(f"Signal sent for {symbol} at {sig_time}")

                # استراحت کوتاه بین درخواست‌ها برای جلوگیری از بلاک شدن
                time.sleep(2) 

            # بعد از اسکن همه نمادها، ۱۵ ثانیه صبر میکنیم
            time.sleep(15) 
            
        except Exception as e:
            print(f"Error in scan: {e}")
            time.sleep(15)

# ==========================================
# ۶. اجرای اصلی (اسکنر + پولینگ تلگرام + وب‌سرور)
# ==========================================
if __name__ == "__main__":
    # ۱. اجرای اسکنر در پس‌زمینه
    t = threading.Thread(target=macd_scanner)
    t.daemon = True
    t.start()
    
    # ۲. اجرای Polling تلگرام در پس‌زمینه (بخش مهمی که باعث میشه /start کار کنه)
    def run_bot_polling():
        try:
            print("Starting Telegram polling...")
            bot.infinity_polling(timeout=20, long_polling_timeout=15)
        except Exception as e:
            print(f"Polling error: {e}")
    
    bot_thread = threading.Thread(target=run_bot_polling)
    bot_thread.daemon = True
    bot_thread.start()
    
    # ۳. اجرای وب‌سرور برای Render (در ترد اصلی)
    run_web()
