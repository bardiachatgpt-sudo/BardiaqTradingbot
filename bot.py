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
import pandas as pd
from datetime import datetime

app = Flask(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
bot = telebot.TeleBot(BOT_TOKEN) if BOT_TOKEN else None

subscribed_users = set()
tehran_tz = pytz.timezone('Asia/Tehran')

def broadcast(text):
    if not bot: return
    for chat_id in list(subscribed_users):
        try:
            bot.send_message(chat_id, text, parse_mode="HTML")
        except Exception as e:
            print(f"Telegram send error: {e}")

def get_current_session():
    now = datetime.now(tehran_tz)
    t = now.hour + now.minute / 60.0
    
    sessions = []
    if 3.5 <= t < 12.5:
        sessions.append("Tokyo")
    if 11.5 <= t < 20.5:
        sessions.append("London")
    if t >= 16.5 or t < 1.5:
        sessions.append("New York")
        
    if not sessions:
        return "Off-Session"
    return " + ".join(sessions)

SYMBOLS = {
    "OANDA:XAUUSD": "XAUUSD",
    "FX:EURUSD": "EURUSD",
    "FX:GBPUSD": "GBPUSD",
    "FX:USDJPY": "USDJPY",
    "FX:AUDUSD": "AUDUSD",
    "FX:USDCAD": "USDCAD",
    "FX:USDCHF": "USDCHF",
    "FX:NZDUSD": "NZDUSD"
}

TIMEFRAMES = {"1": "M1", "5": "M5"}

if bot:
    @bot.message_handler(commands=['start'])
    def send_welcome(message):
        subscribed_users.add(message.chat.id)
        session = get_current_session()
        bot.reply_to(
            message, 
            f"Bot is active.\nCurrent Session: {session}\n\n"
            "Scanning Gold and Majors in M1 and M5.\n"
            "Waiting for the pattern to form. You will be alerted as soon as the 3rd histogram closes.", 
            parse_mode="HTML"
        )

@app.route('/')
def home():
    return "Bot is LIVE!"

def run_web():
    port = int(os.environ.get("PORT", 7860))
    app.run(host="0.0.0.0", port=port)

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
            timeout=10
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
            if len(bars) >= n_bars: 
                break
            
        if ws:
            try: ws.close()
            except: pass
        
        if len(bars) < 150: 
            return None
        df = pd.DataFrame(bars)
        df["Time"] = pd.to_datetime(df["Time"], unit="s", utc=True)
        df.set_index("Time", inplace=True)
        df = df[~df.index.duplicated(keep="last")].sort_index()
        return df
    except Exception as e:
        if ws:
            try: ws.close()
            except Exception: pass
        return None

def check_divergence(df, symbol, tf_name):
    if df is None or len(df) < 150:
        return

    df = df.iloc[:-1] 
    if len(df) < 150:
        return

    exp1 = df['Close'].ewm(span=48, adjust=False).mean()
    exp2 = df['Close'].ewm(span=106, adjust=False).mean()
    macd_line = exp1 - exp2
    signal_line = macd_line.ewm(span=36, adjust=False).mean()
    df['Histogram'] = macd_line - signal_line
    hist = df['Histogram']

    bottoms = []
    tops = []
    middle_peaks = []
    middle_troughs = []

    for i in range(1, len(df) - 1):
        h_prev = hist.iloc[i-1]
        h_curr = hist.iloc[i]
        h_next = hist.iloc[i+1]
        
        if h_curr < 0 and abs(h_curr) > abs(h_prev) and abs(h_curr) > abs(h_next):
            bottoms.append(i)
            
        if h_curr < 0 and abs(h_curr) < abs(h_prev) and abs(h_curr) < abs(h_next):
            middle_peaks.append(i)
            
        if h_curr > 0 and h_curr > h_prev and h_curr > h_next:
            tops.append(i)
            
        if h_curr > 0 and h_curr < h_prev and h_curr < h_next:
            middle_troughs.append(i)

    for i in range(len(bottoms) - 1):
        b1_idx = bottoms[i]
        b2_idx = bottoms[i+1]
        
        if (b2_idx - b1_idx) >= 30:
            has_peak = any(b1_idx < mp < b2_idx for mp in middle_peaks)
            all_neg = (hist.iloc[b1_idx:b2_idx+1] < 0).all()
            
            if has_peak and all_neg:
                b1_hist = hist.iloc[b1_idx]
                b2_hist = hist.iloc[b2_idx]
                b1_low = df['Low'].iloc[b1_idx]
                b2_low = df['Low'].iloc[b2_idx]
                
                if abs(b1_hist) > (abs(b2_hist) * 1.05) and b1_low > b2_low:
                    if b2_idx + 1 == len(df) - 1:
                        dt_tehran = df.index[b2_idx].astimezone(tehran_tz).strftime('%H:%M')
                        session = get_current_session()
                        msg = (
                            f"🚀 <b>Bullish Divergence</b> 🚀\n\n"
                            f"💎 Symbol: <b>{SYMBOLS[symbol]}</b>\n"
                            f"⏳ Timeframe: <b>{tf_name}</b>\n"
                            f"⏰ Time (Tehran): <b>{dt_tehran}</b>\n"
                            f"🌐 Session: <b>{session}</b>\n\n"
                            f"📊 <b>Abs Histogram:</b>\n"
                            f"🔹 Bottom 1: <code>{abs(b1_hist):.6f}</code>\n"
                            f"🔹 Bottom 2: <code>{abs(b2_hist):.6f}</code>\n\n"
                            f"💰 <b>Price (Shadow):</b>\n"
                            f"🔹 Bottom 1: <code>{b1_low:.6f}</code>\n"
                            f"🔹 Bottom 2: <code>{b2_low:.6f}</code>"
                        )
                        broadcast(msg)
                        print(f"Signal Sent: {symbol} {tf_name} Bullish")

    for i in range(len(tops) - 1):
        t1_idx = tops[i]
        t2_idx = tops[i+1]
        
        if (t2_idx - t1_idx) >= 30:
            has_trough = any(t1_idx < mt < t2_idx for mt in middle_troughs)
            all_pos = (hist.iloc[t1_idx:t2_idx+1] > 0).all()
            
            if has_trough and all_pos:
                t1_hist = hist.iloc[t1_idx]
                t2_hist = hist.iloc[t2_idx]
                t1_high = df['High'].iloc[t1_idx]
                t2_high = df['High'].iloc[t2_idx]
                
                if t1_hist > (t2_hist * 1.05) and t1_high < t2_high:
                    if t2_idx + 1 == len(df) - 1:
                        dt_tehran = df.index[t2_idx].astimezone(tehran_tz).strftime('%H:%M')
                        session = get_current_session()
                        msg = (
                            f"📉 <b>Bearish Divergence</b> 📉\n\n"
                            f"💎 Symbol: <b>{SYMBOLS[symbol]}</b>\n"
                            f"⏳ Timeframe: <b>{tf_name}</b>\n"
                            f"⏰ Time (Tehran): <b>{dt_tehran}</b>\n"
                            f"🌐 Session: <b>{session}</b>\n\n"
                            f"📊 <b>Histogram:</b>\n"
                            f"🔹 Top 1: <code>{t1_hist:.6f}</code>\n"
                            f"🔹 Top 2: <code>{t2_hist:.6f}</code>\n\n"
                            f"💰 <b>Price (Shadow):</b>\n"
                            f"🔹 Top 1: <code>{t1_high:.6f}</code>\n"
                            f"🔹 Top 2: <code>{t2_high:.6f}</code>"
                        )
                        broadcast(msg)
                        print(f"Signal Sent: {symbol} {tf_name} Bearish")

def live_scanner():
    while True:
        try:
            if not subscribed_users:
                time.sleep(5)
                continue
                
            for symbol in SYMBOLS.keys():
                for tf_code, tf_name in TIMEFRAMES.items():
                    df = fetch_tradingview_candles(symbol, tf_code, 220)
                    if df is not None:
                        check_divergence(df, symbol, tf_name)
                    time.sleep(1) 
            time.sleep(5) 
        except Exception as e:
            traceback.print_exc()
            time.sleep(10)

if __name__ == "__main__":
    print("Starting bot...")
    t = threading.Thread(target=live_scanner)
    t.daemon = True
    t.start()
    
    if bot:
        def run_bot_polling():
            while True:
                try:
                    bot.infinity_polling(timeout=20, long_polling_timeout=15)
                except Exception as e:
                    time.sleep(10)
        
        bot_thread = threading.Thread(target=run_bot_polling)
        bot_thread.daemon = True
        bot_thread.start()
    
    print("Starting Flask web server...")
    run_web()
