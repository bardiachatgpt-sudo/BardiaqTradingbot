import os
import re
import json
import time
import random
import string
import threading
import traceback
import pytz
import websocket
from flask import Flask
import telebot
from telebot import types
import pandas as pd

# 1. Initial Setup
app = Flask(__name__)

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
                    "price": float(data["price"]),
                    "macd": float(data["macd"]),
                    "p1_macd": float(data.get("p1_macd", 0)),
                    "p1_price": float(data.get("p1_price", 0)),
                    "time": str(data["time"]),
                    "bars_ago": int(data.get("bars_ago", 0))
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
                loaded_divs = data.get("last_divs", {})
                for k, v in loaded_divs.items():
                    v["time"] = pd.to_datetime(v["time"], utc=True)
                    last_divergences[k] = v
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

# 2. Telegram Handlers
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
        "استراتژی: دایورجنس صعودی و نزولی (M1, M5, M15, H1, H4)\n"
        "نمادها: طلا و جفت‌ارزهای ماژور\n"
        "برای دیدن وضعیت، دکمه زیر را بزنید:", 
        reply_markup=get_main_keyboard()
    )

@bot.message_handler(func=lambda message: message.text == '📊 آخرین سیگنال‌ها' or message.text == '/last')
def send_last_signals(message):
    if not last_divergences:
        bot.reply_to(message, "⏳ هنوز دیتایی ثبت نشده. لطفاً صبر کنید تا ربات بازار را اسکن کند.")
        return

    grouped_data = {}
    for key, data in last_divergences.items():
        if "_" in key:
            symbol, tf = key.rsplit("_", 1)
            if symbol not in grouped_data:
                grouped_data[symbol] = {}
            grouped_data[symbol][tf] = data

    msg = "📋 <b>گزارش آخرین دایورجنس‌ها (در ۲۲۰ کندل اخیر):</b>\n\n"
    
    tf_order = {"M1": 1, "M5": 2, "M15": 3, "H1": 4, "H4": 5}
    
    for symbol, tfs in grouped_data.items():
        persian_name = SYMBOLS.get(symbol, symbol)
        msg += f"💎 <b>{persian_name}</b>\n"
        
        sorted_tfs = sorted(tfs.keys(), key=lambda x: tf_order.get(x, 99))
        
        for tf in sorted_tfs:
            data = tfs[tf]
            try:
                dt_tehran = data['time'].astimezone(tehran_tz).strftime('%Y-%m-%d %H:%M')
            except:
                dt_tehran = "نامشخص"
                
            sig_type = "صعودی 🟢" if data['type'] == 'Bullish' else "نزولی 🔴"
            bars_ago = data.get('bars_ago', 0)
            
            msg += f"  ⏱ <b>{tf}</b>: {sig_type} | {dt_tehran} | ({bars_ago} کندل قبل)\n"
            
        msg += "\n"
        
    for i in range(0, len(msg), 4000):
        bot.reply_to(message, msg[i:i+4000], parse_mode="HTML")

@app.route('/')
def home():
    return "MACD Bot is LIVE!"

def run_web():
    port = int(os.environ.get("PORT", 7860))
    app.run(host="0.0.0.0", port=port)

# 3. TradingView WebSocket Connection
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
            timeout=8
        )
        chart_session = "cs" + "".join(random.choices(string.ascii_lowercase + string.digits, k=12))
        tv_send(ws, "set_auth_token", ["unauthorized_user_token"])
        tv_send(ws, "chart_create_session", [chart_session, ""])
        sym_json = json.dumps({"symbol": symbol, "adjustment": "splits"}, separators=(",", ":"))
        tv_send(ws, "resolve_symbol", [chart_session, "sds_sym_1", f"={sym_json}"])
        tv_send(ws, "create_series", [chart_session, "sds_1", "s1", "sds_sym_1", str(interval), n_bars, ""])
        
        bars = []
        start_t = time.time()
        while time.time() - start_t < 8:
            try:
                raw = ws.recv()
            except Exception:
                break
                
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
            
        if ws:
            try: ws.close()
            except: pass
        
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

# 4. Strategy Logic
def detect_macd_divergence(df):
    if df is None or len(df) < 10:
        return []

    exp1 = df['Close'].ewm(span=48, adjust=False).mean()
    exp2 = df['Close'].ewm(span=106, adjust=False).mean()
    df['MACD'] = exp1 - exp2
    df['Slope'] = df['MACD'].diff() 

    signals = []
    total_bars = len(df)
    
    b_state = 0
    b_p1_macd, b_p1_low, b_p1_idx = None, None, None
    
    s_state = 0
    s_p1_macd, s_p1_high, s_p1_idx = None, None, None

    for i in range(2, len(df)):
        current_slope = df['Slope'].iloc[i]
        prev_slope = df['Slope'].iloc[i-1]
        
        # Bullish Divergence
        if b_state == 0:
            if prev_slope < 0 and current_slope > 0:
                macd_val = df['MACD'].iloc[i-1]
                if macd_val < 0: 
                    b_p1_macd = macd_val
                    b_p1_low = df['Low'].iloc[i-1]
                    b_p1_idx = i-1
                    b_state = 1
        elif b_state == 1:
            if prev_slope > 0 and current_slope < 0:
                macd_val = df['MACD'].iloc[i-1]
                if macd_val < 0:
                    b_state = 2
                else:
                    b_state = 0 
        elif b_state == 2:
            if prev_slope < 0 and current_slope > 0:
                macd_val = df['MACD'].iloc[i-1]
                if macd_val < 0:
                    all_below = (df['MACD'].iloc[b_p1_idx:i] < 0).all()
                    if all_below:
                        cond_macd = abs(b_p1_macd) > abs(macd_val)
                        cond_price = b_p1_low > df['Low'].iloc[i-1]
                        if cond_macd and cond_price:
                            bars_ago = total_bars - 1 - (i-1)
                            signals.append({
                                "type": "Bullish", 
                                "price": df['Low'].iloc[i-1], 
                                "macd": macd_val,
                                "p1_price": b_p1_low,
                                "p1_macd": b_p1_macd, 
                                "time": df.index[i-1],
                                "bars_ago": bars_ago
                            })
                    b_state = 0

        # Bearish Divergence
        if s_state == 0:
            if prev_slope > 0 and current_slope < 0:
                macd_val = df['MACD'].iloc[i-1]
                if macd_val > 0:
                    s_p1_macd = macd_val
                    s_p1_high = df['High'].iloc[i-1]
                    s_p1_idx = i-1
                    s_state = 1
        elif s_state == 1:
            if prev_slope < 0 and current_slope > 0:
                macd_val = df['MACD'].iloc[i-1]
                if macd_val > 0:
                    s_state = 2
                else:
                    s_state = 0 
        elif s_state == 2:
            if prev_slope > 0 and current_slope < 0:
                macd_val = df['MACD'].iloc[i-1]
                if macd_val > 0:
                    all_above = (df['MACD'].iloc[s_p1_idx:i] > 0).all()
                    if all_above:
                        cond_macd = s_p1_macd > macd_val
                        cond_price = s_p1_high < df['High'].iloc[i-1]
                        if cond_macd and cond_price:
                            bars_ago = total_bars - 1 - (i-1)
                            signals.append({
                                "type": "Bearish", 
                                "price": df['High'].iloc[i-1],
                                "macd": macd_val,
                                "p1_price": s_p1_high,
                                "p1_macd": s_p1_macd, 
                                "time": df.index[i-1],
                                "bars_ago": bars_ago
                            })
                    s_state = 0

    return signals

# 5. Scanner Loop
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

TIMEFRAMES = {"1": "M1", "5": "M5", "15": "M15", "60": "H1", "240": "H4"}
last_signal_times = {}

def macd_scanner():
    global last_source_name, last_divergences
    while True:
        try:
            if not subscribed_users:
                time.sleep(5)
                continue
                
            for symbol, persian_name in SYMBOLS.items():
                for tf_code, tf_name in TIMEFRAMES.items():
                    df, err = fetch_tradingview_candles(symbol, tf_code, 220)
                    
                    if df is None:
                        continue
                    
                    signals = detect_macd_divergence(df)
                    
                    if signals:
                        latest_sig = signals[-1] 
                        sig_time = latest_sig["time"]
                        key = f"{symbol}_{tf_name}"
                        
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

                        now_utc = pd.Timestamp.now(tz='UTC')
                        time_diff = (now_utc - sig_time).total_seconds()
                        live_threshold = 90 if tf_name == "M1" else (300 if tf_name == "M5" else (900 if tf_name == "M15" else 3600))
                        
                        if 0 <= time_diff <= live_threshold: 
                            if last_signal_times.get(key) != sig_time:
                                current_macd = df['MACD'].iloc[-1]
                                
                                if latest_sig['type'] == "Bullish" and current_macd >= 0:
                                    continue
                                if latest_sig['type'] == "Bearish" and current_macd <= 0:
                                    continue
                                    
                                last_signal_times[key] = sig_time
                                dt_tehran = sig_time.astimezone(tehran_tz).strftime('%H:%M')
                                
                                if latest_sig['type'] == "Bullish":
                                    msg = (
                                        f"🚀 <b>دایورجنس صعودی MACD</b>\n\n"
                                        f"💎 نماد: <b>{persian_name}</b>\n"
                                        f"⏳ تایم‌فریم: <b>{tf_name}</b>\n"
                                        f"⏰ زمان (تهران): <b>{dt_tehran}</b>"
                                    )
                                else:
                                    msg = (
                                        f"📉 <b>دایورجنس نزولی MACD</b>\n\n"
                                        f"💎 نماد: <b>{persian_name}</b>\n"
                                        f"⏳ تایم‌فریم: <b>{tf_name}</b>\n"
                                        f"⏰ زمان (تهران): <b>{dt_tehran}</b>"
                                    )
                                broadcast(msg)

                    time.sleep(0.5) 
            time.sleep(5) 
        except Exception as e:
            print(f"Error in scanner: {e}")
            traceback.print_exc()
            time.sleep(10)

# 6. Main Execution
if __name__ == "__main__":
    t = threading.Thread(target=macd_scanner)
    t.daemon = True
    t.start()
    
    def run_bot_polling():
        while True:
            try:
                bot.infinity_polling(timeout=20, long_polling_timeout=15)
            except Exception as e:
                print(f"Polling Error: {e}")
                time.sleep(10)
    
    bot_thread = threading.Thread(target=run_bot_polling)
    bot_thread.daemon = True
    bot_thread.start()
    
    run_web()
