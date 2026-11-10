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
                    "p1_price": data.get("p1_price", 0),
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
        "✅ ربات اسکنر MACD روشن شد.\n\n"
        "استراتژی: دایورجنس صعودی و نزولی (M1, M5, M15)\n"
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
    try:
        sorted_divs = sorted(last_divergences.items(), key=lambda x: pd.to_datetime(x[1]['time']), reverse=True)
    except:
        sorted_divs = list(last_divergences.items())
    
    for key, data in sorted_divs:
        try:
            dt_utc = pd.to_datetime(data['time'], utc=True)
            dt_tehran = dt_utc.astimezone(tehran_tz).strftime('%Y-%m-%d %H:%M')
        except:
            dt_tehran = "نامشخص"
        
        signal_type = "صعودی 🟢" if data['type'] == 'Bullish' else "نزولی 🔴"
        
        msg += (
            f"💎 <b>{key}</b> | {signal_type}\n"
            f"⏰ زمان: {dt_tehran} (تهران)\n"
            f"📉 مکدی: کف/سقف اول {data.get('p1_macd', 0):.4f} | کف/سقف دوم {data['macd']:.4f}\n"
            f"💰 قیمت: اول {data.get('p1_price', 0):.5f} | دوم {data['price']:.5f}\n\n"
        )
    
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
# ۴. منطق استراتژی (صعودی و نزولی)
# ==========================================
def detect_macd_divergence(df):
    if df is None or len(df) < 10:
        return []

    exp1 = df['Close'].ewm(span=48, adjust=False).mean()
    exp2 = df['Close'].ewm(span=106, adjust=False).mean()
    df['MACD'] = exp1 - exp2
    df['Slope'] = df['MACD'].diff() 

    signals = []
    
    # --- استیت ماشین برای دایورجنس صعودی ---
    b_state = 0
    b_p1_macd, b_p1_price, b_p1_idx = None, None, None
    
    # --- استیت ماشین برای دایورجنس نزولی ---
    s_state = 0
    s_p1_macd, s_p1_price, s_p1_idx = None, None, None

    for i in range(2, len(df)):
        current_slope = df['Slope'].iloc[i]
        prev_slope = df['Slope'].iloc[i-1]
        
        # ==========================================
        # ۱. بررسی دایورجنس صعودی (کف‌ها - فاز منفی)
        # ==========================================
        if b_state == 0:
            if prev_slope < 0 and current_slope >= 0:
                macd_val = df['MACD'].iloc[i-1]
                if macd_val < 0:
                    b_p1_macd = macd_val
                    b_p1_price = df['Close'].iloc[i-1]
                    b_p1_idx = i-1
                    b_state = 1

        elif b_state == 1:
            if prev_slope > 0 and current_slope <= 0:
                macd_val = df['MACD'].iloc[i-1]
                if macd_val < 0:
                    b_state = 2
                else:
                    b_state = 0 

        elif b_state == 2:
            if prev_slope < 0 and current_slope >= 0:
                macd_val = df['MACD'].iloc[i-1]
                if macd_val < 0:
                    all_below_zero = (df['MACD'].iloc[b_p1_idx:i] < 0).all()
                    if all_below_zero:
                        cond_macd = abs(b_p1_macd) > abs(macd_val)
                        cond_price = b_p1_price > df['Close'].iloc[i-1]
                        if cond_macd and cond_price:
                            signals.append({
                                "type": "Bullish", 
                                "price": df['Close'].iloc[i-1], 
                                "macd": macd_val,
                                "p1_price": b_p1_price,
                                "p1_macd": b_p1_macd, 
                                "time": df.index[i-1]
                            })
                    b_state = 0

        # ==========================================
        # ۲. بررسی دایورجنس نزولی (سقف‌ها - فاز مثبت)
        # ==========================================
        if s_state == 0:
            # سقف اول: شیب از مثبت به منفی تغییر کرد
            if prev_slope > 0 and current_slope <= 0:
                macd_val = df['MACD'].iloc[i-1]
                if macd_val > 0: # شرط فاز مثبت
                    s_p1_macd = macd_val
                    s_p1_price = df['Close'].iloc[i-1]
                    s_p1_idx = i-1
                    s_state = 1

        elif s_state == 1:
            # کف میانی: شیب از منفی به مثبت تغییر کرد
            if prev_slope < 0 and current_slope >= 0:
                macd_val = df['MACD'].iloc[i-1]
                if macd_val > 0: # کف میانی نباید زیر صفر بره
                    s_state = 2
                else:
                    s_state = 0 

        elif s_state == 2:
            # سقف دوم: شیب دوباره از مثبت به منفی تغییر کرد
            if prev_slope > 0 and current_slope <= 0:
                macd_val = df['MACD'].iloc[i-1]
                if macd_val > 0:
                    all_above_zero = (df['MACD'].iloc[s_p1_idx:i] > 0).all()
                    if all_above_zero:
                        # سقف دوم باید ضعیف‌تر باشه (مقدار مکدی کمتر) و قیمت بالاتر
                        cond_macd = s_p1_macd > macd_val
                        cond_price = s_p1_price < df['Close'].iloc[i-1]
                        if cond_macd and cond_price:
                            signals.append({
                                "type": "Bearish", 
                                "price": df['Close'].iloc[i-1], 
                                "macd": macd_val,
                                "p1_price": s_p1_price,
                                "p1_macd": s_p1_macd, 
                                "time": df.index[i-1]
                            })
                    s_state = 0

    return signals

# ==========================================
# ۵. حلقه اسکنر
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

# تغییر تایم‌فریم‌ها به M1, M5, M15
TIMEFRAMES = {"1": "M1", "5": "M5", "15": "M15"}
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
                    df, err = fetch_tradingview_candles(symbol, tf_code, 220)
                    
                    if df is None:
                        continue
                    
                    signals = detect_macd_divergence(df)
                    
                    if signals:
                        # آخرین سیگنال (چه صعودی چه نزولی) رو جدا می‌کنیم
                        latest_sig = signals[-1] 
                        sig_time = latest_sig["time"]
                        key = f"{symbol}_{tf_name}"
                        
                        # ذخیره در حافظه
                        stored_sig = last_divergences.get(key)
                        is_newer = False
                        if not stored_sig:
                            is_newer = True
                        else:
                            try:
                                if sig_time > pd.to_datetime(stored_sig['time'], utc=True):
                                    is_newer = True
                            except:
                                is_newer = True
                                
                        if is_newer:
                            last_divergences[key] = latest_sig
                            save_state()

                        # ارسال پیام
                        now_utc = pd.Timestamp.now(tz='UTC')
                        time_diff = (now_utc - sig_time).total_seconds()
                        
                        # استانه زمانی بر اساس تایم‌فریم
                        live_threshold = 90 if tf_name == "M1" else (300 if tf_name == "M5" else 900)
                        
                        if time_diff <= live_threshold: 
                            if last_signal_times.get(key) != sig_time:
                                current_macd = df['MACD'].iloc[-1]
                                
                                # فیلتر نهایی: برای صعودی مکدی باید منفی باشه، برای نزولی باید مثبت باشه
                                if latest_sig['type'] == "Bullish" and current_macd >= 0:
                                    continue
                                if latest_sig['type'] == "Bearish" and current_macd <= 0:
                                    continue
                                    
                                last_signal_times[key] = sig_time
                                dt_tehran = sig_time.astimezone(tehran_tz).strftime('%H:%M:%S')
                                
                                if latest_sig['type'] == "Bullish":
                                    msg = (
                                        f"🚀 <b>دایورجنس صعودی MACD</b> 🚀\n"
                                        f"💎 نماد: <b>{persian_name}</b> | ⏳ تایم: <b>{tf_name}</b>\n\n"
                                        f"⏰ زمان (تهران): {dt_tehran}\n\n"
                                        f"📊 <b>بررسی شرط ۱ (قدر مطلق MACD):</b>\n"
                                        f"🔹 کف اول (Abs): <code>{abs(latest_sig['p1_macd']):.4f}</code>\n"
                                        f"🔹 کف دوم (Abs): <code>{abs(latest_sig['macd']):.4f}</code>\n"
                                        f"✅ <i>کف اول بزرگتر است (ضعف مومنتوم نزولی)</i>\n\n"
                                        f"💰 <b>بررسی شرط ۲ (قیمت):</b>\n"
                                        f"🔹 قیمت کف اول: <code>{latest_sig['p1_price']:.5f}</code>\n"
                                        f"🔹 قیمت کف دوم: <code>{latest_sig['price']:.5f}</code>\n"
                                        f"✅ <i>قیمت کف دوم پایین‌تر است</i>"
                                    )
                                else:
                                    msg = (
                                        f"📉 <b>دایورجنس نزولی MACD</b> 📉\n"
                                        f"💎 نماد: <b>{persian_name}</b> | ⏳ تایم: <b>{tf_name}</b>\n\n"
                                        f"⏰ زمان (تهران): {dt_tehran}\n\n"
                                        f"📊 <b>بررسی شرط ۱ (مقدار MACD):</b>\n"
                                        f"🔹 سقف اول: <code>{latest_sig['p1_macd']:.4f}</code>\n"
                                        f"🔹 سقف دوم: <code>{latest_sig['macd']:.4f}</code>\n"
                                        f"✅ <i>سقف اول بزرگتر است (ضعف مومنتوم صعودی)</i>\n\n"
                                        f"💰 <b>بررسی شرط ۲ (قیمت):</b>\n"
                                        f"🔹 قیمت سقف اول: <code>{latest_sig['p1_price']:.5f}</code>\n"
                                        f"🔹 قیمت سقف دوم: <code>{latest_sig['price']:.5f}</code>\n"
                                        f"✅ <i>قیمت سقف دوم بالاتر است</i>"
                                    )
                                broadcast(msg)

                    time.sleep(1) 
            time.sleep(10) 
        except Exception as e:
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
            bot.infinity_polling(timeout=20, long_polling_timeout=15)
        except Exception as e:
            pass
    
    bot_thread = threading.Thread(target=run_bot_polling)
    bot_thread.daemon = True
    bot_thread.start()
    
    run_web()
