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
# ۱. تنظیمات اولیه و وب‌سرور
# ==========================================
app = Flask(__name__)
last_source_name = "در حال اتصال..."

BOT_TOKEN = os.environ.get("BOT_TOKEN")
bot = telebot.TeleBot(BOT_TOKEN) if BOT_TOKEN else None

STATE_FILE = "bot_state.json"
subscribed_users = set()
# دیکشنری برای ذخیره سیگنال‌ها بر اساس نماد و تایم‌فریم
# ساختار: { "XAUUSD_M1": {"type": "Bullish", "price": ..., "time": ...}, ... }
last_divergences = {} 

tehran_tz = pytz.timezone('Asia/Tehran')

def save_state():
    try:
        with open(STATE_FILE, "w") as f:
            serializable_divs = {}
            for key, data in last_divergences.items():
                serializable_divs[key] = {
                    "type": data["type"],
                    "price": data["price"],
                    "macd": data["macd"],
                    "p1_macd": data.get("p1_macd", 0),
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
# ۲. هندلرهای ربات تلگرام
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
        "✅ ربات اسکنر MACD (مولتی‌تایم‌فریم) روشن شد.\n\n"
        "استراتژی: دایورجنس (M1, M5, H1)\n"
        "نمادها: طلا و جفت‌ارزهای ماژور\n"
        "برای دیدن وضعیت، دکمه زیر را بزنید:", 
        reply_markup=get_main_keyboard()
    )

@bot.message_handler(func=lambda message: message.text == '📊 آخرین سیگنال‌ها' or message.text == '/last')
def send_last_signals(message):
    if not last_divergences:
        bot.reply_to(message, "⏳ هنوز دیتایی ثبت نشده. لطفاً صبر کنید تا ربات بازار را اسکن کند.")
        return

    msg = "📋 <b>آخرین دایورجنس‌ها (تفکیک تایم‌فریم):</b>\n\n"
    
    # مرتب کردن بر اساس زمان (جدیدترین‌ها اول)
    try:
        sorted_divs = sorted(last_divergences.items(), key=lambda x: pd.to_datetime(x[1]['time']), reverse=True)
    except:
        sorted_divs = list(last_divergences.items())
    
    for key, data in sorted_divs:
        # key مثلا میشه: OANDA:XAUUSD_M1
        try:
            dt_utc = pd.to_datetime(data['time'], utc=True)
            dt_tehran = dt_utc.astimezone(tehran_tz).strftime('%Y-%m-%d %H:%M')
        except:
            dt_tehran = "نامشخص"
            
        msg += (
            f"💎 <b>{key}</b>\n"
            f"⏰ زمان: {dt_tehran} (تهران)\n"
            f"💰 قیمت: {data['price']}\n"
            f"📉 MACD دوم: {data['macd']:.4f} | MACD اول: {data.get('p1_macd', 0):.4f}\n\n"
        )
    
    # ارسال به صورت تکه‌تکه اگر طولانی شد (محدودیت تلگرام 4096 کاراکتر)
    for i in range(0, len(msg), 4000):
        bot.reply_to(message, msg[i:i+4000], parse_mode="HTML")

@app.route('/')
def home():
    return "MACD Multi-Timeframe Bot is LIVE!"

def run_web():
    port = int(os.environ.get("PORT", 7860))
    app.run(host="0.0.0.0", port=port)

# ==========================================
# ۳. موتور اتصال به تریدینگ‌ویو
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
# ۴. منطق استراتژی (با فیلتر نویز سخت‌گیرانه)
# ==========================================
def detect_macd_divergence(df):
    if df is None or len(df) < 10:
        return []

    exp1 = df['Close'].ewm(span=48, adjust=False).mean()
    exp2 = df['Close'].ewm(span=106, adjust=False).mean()
    df['MACD'] = exp1 - exp2
    df['Slope'] = df['MACD'].diff()

    state = 0
    p1_price, p1_macd, p1_idx = None, None, None
    signals = []

    # آستانه برای جلوگیری از نویز (تغییرات خیلی ریز شیب)
    MIN_SLOPE_CHANGE = 0.0001

    for i in range(2, len(df)):
        current_slope = df['Slope'].iloc[i]
        prev_slope = df['Slope'].iloc[i-1]
        
        if state == 0:
            # کف اول: شیب از منفی به مثبت تغییر کرده (و تغییرش محسوس باشه)
            if prev_slope < -MIN_SLOPE_CHANGE and current_slope >= 0:
                if df['MACD'].iloc[i] < 0:
                    p1_price = df['Close'].iloc[i]
                    p1_macd = df['MACD'].iloc[i]
                    p1_idx = i
                    state = 1

        elif state == 1:
            # سقف میانی: شیب از مثبت به منفی تغییر کرده
            if prev_slope > MIN_SLOPE_CHANGE and current_slope <= 0:
                if df['MACD'].iloc[i] < 0:
                    state = 2
                else:
                    state = 0

        elif state == 2:
            # کف دوم: شیب دوباره منفی به مثبت شده
            if prev_slope < -MIN_SLOPE_CHANGE and current_slope >= 0:
                if df['MACD'].iloc[i] < 0:
                    p2_price = df['Close'].iloc[i]
                    p2_macd = df['MACD'].iloc[i]
                    
                    # چک میکنیم آیا در تمام این مدت، مکدی زیر صفر بوده؟
                    all_below_zero = (df['MACD'].iloc[p1_idx:i] < 0).all()
                    
                    if all_below_zero:
                        cond_macd = abs(p1_macd) > abs(p2_macd)
                        cond_price = p1_price > p2_price
                        
                        if cond_macd and cond_price:
                            signals.append({
                                "type": "Bullish", 
                                "price": p2_price, 
                                "macd": p2_macd,
                                "p1_macd": p1_macd, 
                                "time": df.index[i]
                            })
                    state = 0
    return signals

# ==========================================
# ۵. حلقه اسکنر (چند تایم‌فریمه)
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

# تایم‌فریم‌ها: M1 -> "1" ، M5 -> "5" ، H1 -> "60"
TIMEFRAMES = {
    "1": "M1",
    "5": "M5",
    "60": "H1"
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
                for tf_code, tf_name in TIMEFRAMES.items():
                    # برای هر نماد و هر تایم‌فریم، دیتا رو می‌گیریم
                    df, err = fetch_tradingview_candles(symbol, tf_code, 220)
                    
                    if df is None:
                        print(f"Error fetching {symbol} {tf_name}: {err}")
                        continue
                    
                    signals = detect_macd_divergence(df)
                    
                    if signals:
                        latest_sig = signals[-1] 
                        sig_time = latest_sig["time"]
                        key = f"{symbol}_{tf_name}"
                        
                        # ۱. ذخیره در حافظه برای دکمه (مهم نیست چقدر قدیمی باشه)
                        stored_sig = last_divergences.get(key)
                        is_newer = False
                        if not stored_sig:
                            is_newer = True
                        else:
                            try:
                                stored_time = pd.to_datetime(stored_sig['time'], utc=True)
                                if sig_time > stored_time:
                                    is_newer = True
                            except:
                                is_newer = True
                                
                        if is_newer:
                            last_divergences[key] = latest_sig
                            save_state()

                        # ۲. ارسال پیام (فقط برای سیگنال‌های زنده)
                        now_utc = pd.Timestamp.now(tz='UTC')
                        time_diff = (now_utc - sig_time).total_seconds()
                        
                        # اگر سیگنال در چند ثانیه/دقیقه اخیر رخ داده باشه
                        live_threshold = 90 if tf_name == "M1" else (300 if tf_name == "M5" else 3600)
                        
                        if time_diff <= live_threshold: 
                            if last_signal_times.get(key) != sig_time:
                                
                                # چک نهایی: آیا همین الان مکدی منفی است؟
                                current_macd = df['MACD'].iloc[-1]
                                if current_macd >= 0:
                                    print(f"⚠️ سیگنال فیلتر شد برای {key}: مکدی فعلی مثبت است ({current_macd:.4f})")
                                    continue 
                                    
                                last_signal_times[key] = sig_time
                                dt_tehran = sig_time.astimezone(tehran_tz).strftime('%H:%M:%S')
                                
                                msg = (
                                    f"🚀 <b>دایورجنس صعودی MACD</b> 🚀\n"
                                    f"💎 نماد: <b>{persian_name}</b> | ⏳ تایم: <b>{tf_name}</b>\n\n"
                                    f"📡 چارت: <code>{symbol}</code>\n"
                                    f"💰 قیمت سیگنال: <code>{latest_sig['price']:.5f}</code>\n"
                                    f"⏰ زمان (تهران): {dt_tehran}\n\n"
                                    f"📊 <b>مقادیر مکدی:</b>\n"
                                    f"🔹 مکدی کف اول: <code>{latest_sig['p1_macd']:.4f}</code>\n"
                                    f"🔹 مکدی کف دوم: <code>{latest_sig['macd']:.4f}</code>\n"
                                    f"🔹 مکدی فعلی: <code>{current_macd:.4f}</code>\n\n"
                                    f"📌 <i>طبق استراتژی: کف دوم مکدی ضعیف‌تر، اما قیمت پایین‌تر.</i>"
                                )
                                broadcast(msg)
                                print(f"Live signal broadcasted for {key} at {sig_time}")

                    time.sleep(1) # استراحت بین تایم‌فریم‌ها
                    
            time.sleep(10) # استراحت کل اسکن
            
        except Exception as e:
            print(f"Error in scan: {e}")
            time.sleep(15)

# ==========================================
# ۶. اجرای اصلی
# ==========================================
if __name__ == "__main__":
    t = threading.Thread(target=macd_scanner)
    t.daemon = True
    t.start()
    
    def run_bot_polling():
        try:
            print("Starting Telegram polling...")
            bot.infinity_polling(timeout=20, long_polling_timeout=15)
        except Exception as e:
            print(f"Polling error: {e}")
    
    bot_thread = threading.Thread(target=run_bot_polling)
    bot_thread.daemon = True
    bot_thread.start()
    
    run_web()
