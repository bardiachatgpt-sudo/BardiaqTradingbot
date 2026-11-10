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
    if 3.5 <= t < 12.5: sessions.append("Tokyo")
    if 11.5 <= t < 20.5: sessions.append("London")
    if t >= 16.5 or t < 1.5: sessions.append("New York")
    if not sessions: return "Off-Session"
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
        bot.reply_to(
            message, 
            "Bot is active.\n"
            "Use /status to see the current phase and what I am waiting for on every pair.", 
            parse_mode="HTML"
        )

    @bot.message_handler(commands=['status'])
    def send_status(message):
        bot.reply_to(message, "Fetching live data. Please wait...")
        status_msg = "📊 <b>Current Market Status:</b>\n\n"
        
        for symbol in SYMBOLS.keys():
            for tf_code, tf_name in TIMEFRAMES.items():
                df = fetch_tradingview_candles(symbol, tf_code, 220)
                if df is not None:
                    state_text = analyze_current_state(df)
                    status_msg += f"💎 <b>{SYMBOLS[symbol]}</b> ({tf_name}):\n{state_text}\n\n"
        
        for i in range(0, len(status_msg), 4000):
            bot.send_message(message.chat.id, status_msg[i:i+4000], parse_mode="HTML")

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
            try: raw = ws.recv()
            except Exception: break
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
            if len(bars) >= n_bars: break
            
        if ws:
            try: ws.close()
            except: pass
        
        if len(bars) < 150: return None
        df = pd.DataFrame(bars)
        df["Time"] = pd.to_datetime(df["Time"], unit="s", utc=True)
        df.set_index("Time", inplace=True)
        df = df[~df.index.duplicated(keep="last")].sort_index()
        return df
    except Exception:
        if ws:
            try: ws.close()
            except Exception: pass
        return None

def get_histogram(df):
    df_closed = df.iloc[:-1].copy()
    if len(df_closed) < 150: return None, None
    exp1 = df_closed['Close'].ewm(span=48, adjust=False).mean()
    exp2 = df_closed['Close'].ewm(span=106, adjust=False).mean()
    macd_line = exp1 - exp2
    signal_line = macd_line.ewm(span=36, adjust=False).mean()
    df_closed['Histogram'] = macd_line - signal_line
    return df_closed, df_closed['Histogram'].tolist()

def analyze_current_state(df):
    df_closed, hist = get_histogram(df)
    if df_closed is None: return "Not enough data."
    
    last_hist = hist[-1]
    
    if last_hist < 0:
        phase_start = 0
        for i in range(len(hist)-1, -1, -1):
            if hist[i] > 0:
                phase_start = i
                break
                
        phase_hist = hist[phase_start:]
        
        b1_val = min(phase_hist)
        b1_idx = phase_start + phase_hist.index(b1_val)
        
        if b1_idx == len(hist) - 1:
            return f"🔴 <b>Phase: Negative</b>\nStill dropping. Bottom 1 forming at {df_closed['Low'].iloc[b1_idx]:.5f} (Abs: {abs(b1_val):.6f}).\nWaiting for retracement..."
            
        post_b1 = hist[b1_idx:]
        peak_val = max(post_b1)
        peak_idx = b1_idx + post_b1.index(peak_val)
        
        if peak_idx == len(hist) - 1:
            return f"🔴 <b>Phase: Negative</b>\nBottom 1: {df_closed['Low'].iloc[b1_idx]:.5f} (Abs: {abs(b1_val):.6f})\nMiddle Peak forming at {df_closed['Low'].iloc[peak_idx]:.5f} (Abs: {abs(peak_val):.6f}).\nWaiting for it to finish..."
            
        post_peak = hist[peak_idx:]
        b2_val = min(post_peak)
        b2_idx = peak_idx + post_peak.index(b2_val)
        
        if b2_idx == len(hist) - 1:
            return f"🔴 <b>Phase: Negative</b>\nBottom 1: {df_closed['Low'].iloc[b1_idx]:.5f} (Abs: {abs(b1_val):.6f})\nMiddle Peak: {df_closed['Low'].iloc[peak_idx]:.5f} (Abs: {abs(peak_val):.6f})\nNew Bottom forming at {df_closed['Low'].iloc[b2_idx]:.5f} (Abs: {abs(b2_val):.6f}).\nWatching for confirmation..."
            
        return f"🔴 <b>Phase: Negative</b>\nBottom 1: {df_closed['Low'].iloc[b1_idx]:.5f} (Abs: {abs(b1_val):.6f})\nMiddle Peak: {df_closed['Low'].iloc[peak_idx]:.5f} (Abs: {abs(peak_val):.6f})\nBottom 2 formed at {df_closed['Low'].iloc[b2_idx]:.5f} (Abs: {abs(b2_val):.6f})."
        
    elif last_hist > 0:
        phase_start = 0
        for i in range(len(hist)-1, -1, -1):
            if hist[i] < 0:
                phase_start = i
                break
                
        phase_hist = hist[phase_start:]
        
        t1_val = max(phase_hist)
        t1_idx = phase_start + phase_hist.index(t1_val)
        
        if t1_idx == len(hist) - 1:
            return f"🟢 <b>Phase: Positive</b>\nStill rising. Top 1 forming at {df_closed['High'].iloc[t1_idx]:.5f} (Val: {t1_val:.6f}).\nWaiting for pullback..."
            
        post_t1 = hist[t1_idx:]
        trough_val = min(post_t1)
        trough_idx = t1_idx + post_t1.index(trough_val)
        
        if trough_idx == len(hist) - 1:
            return f"🟢 <b>Phase: Positive</b>\nTop 1: {df_closed['High'].iloc[t1_idx]:.5f} (Val: {t1_val:.6f})\nMiddle Trough forming at {df_closed['High'].iloc[trough_idx]:.5f} (Val: {trough_val:.6f}).\nWaiting for it to finish..."
            
        post_trough = hist[trough_idx:]
        t2_val = max(post_trough)
        t2_idx = trough_idx + post_trough.index(t2_val)
        
        if t2_idx == len(hist) - 1:
            return f"🟢 <b>Phase: Positive</b>\nTop 1: {df_closed['High'].iloc[t1_idx]:.5f} (Val: {t1_val:.6f})\nMiddle Trough: {df_closed['High'].iloc[trough_idx]:.5f} (Val: {trough_val:.6f})\nNew Top forming at {df_closed['High'].iloc[t2_idx]:.5f} (Val: {t2_val:.6f}).\nWatching for confirmation..."
            
        return f"🟢 <b>Phase: Positive</b>\nTop 1: {df_closed['High'].iloc[t1_idx]:.5f} (Val: {t1_val:.6f})\nMiddle Trough: {df_closed['High'].iloc[trough_idx]:.5f} (Val: {trough_val:.6f})\nTop 2 formed at {df_closed['High'].iloc[t2_idx]:.5f} (Val: {t2_val:.6f})."
        
    return "Phase unclear."

def check_divergence(df, symbol, tf_name):
    df_closed, hist = get_histogram(df)
    if df_closed is None or len(hist) < 150: return

    last_hist = hist[-1]
    
    if last_hist < 0:
        phase_start = 0
        for i in range(len(hist)-1, -1, -1):
            if hist[i] > 0:
                phase_start = i
                break
                
        phase_hist = hist[phase_start:]
        b1_val = min(phase_hist)
        b1_idx = phase_start + phase_hist.index(b1_val)
        
        if b1_idx == len(hist) - 1: return 
            
        post_b1 = hist[b1_idx:]
        peak_val = max(post_b1)
        peak_idx = b1_idx + post_b1.index(peak_val)
        
        if peak_idx == len(hist) - 1: return 
            
        post_peak = hist[peak_idx:]
        b2_val = min(post_peak)
        b2_idx = peak_idx + post_peak.index(b2_val)
        
        if b2_idx == len(hist) - 2:
            if (b2_idx - b1_idx) >= 30 and abs(b1_val) > abs(b2_val) and df_closed['Low'].iloc[b1_idx] > df_closed['Low'].iloc[b2_idx]:
                dt_tehran = df_closed.index[b2_idx].astimezone(tehran_tz).strftime('%H:%M')
                session = get_current_session()
                msg = (
                    f"🚀 <b>Bullish Divergence CONFIRMED</b> 🚀\n\n"
                    f"💎 Symbol: <b>{SYMBOLS[symbol]}</b>\n"
                    f"⏳ Timeframe: <b>{tf_name}</b>\n"
                    f"⏰ Time (Tehran): <b>{dt_tehran}</b>\n"
                    f"🌐 Session: <b>{session}</b>\n\n"
                    f"📊 <b>Abs Histogram:</b>\n"
                    f"🔹 Bottom 1: <code>{abs(b1_val):.6f}</code>\n"
                    f"🔹 Middle Peak: <code>{abs(peak_val):.6f}</code>\n"
                    f"🔹 Bottom 2: <code>{abs(b2_val):.6f}</code>\n\n"
                    f"💰 <b>Price (Shadow):</b>\n"
                    f"🔹 Bottom 1: <code>{df_closed['Low'].iloc[b1_idx]:.6f}</code>\n"
                    f"🔹 Bottom 2: <code>{df_closed['Low'].iloc[b2_idx]:.6f}</code>"
                )
                broadcast(msg)
                print(f"Signal Sent: {symbol} {tf_name} Bullish")
                return

    elif last_hist > 0:
        phase_start = 0
        for i in range(len(hist)-1, -1, -1):
            if hist[i] < 0:
                phase_start = i
                break
                
        phase_hist = hist[phase_start:]
        t1_val = max(phase_hist)
        t1_idx = phase_start + phase_hist.index(t1_val)
        
        if t1_idx == len(hist) - 1: return
            
        post_t1 = hist[t1_idx:]
        trough_val = min(post_t1)
        trough_idx = t1_idx + post_t1.index(trough_val)
        
        if trough_idx == len(hist) - 1: return
            
        post_trough = hist[trough_idx:]
        t2_val = max(post_trough)
        t2_idx = trough_idx + post_trough.index(t2_val)
        
        if t2_idx == len(hist) - 2:
            if (t2_idx - t1_idx) >= 30 and t1_val > t2_val and df_closed['High'].iloc[t1_idx] < df_closed['High'].iloc[t2_idx]:
                dt_tehran = df_closed.index[t2_idx].astimezone(tehran_tz).strftime('%H:%M')
                session = get_current_session()
                msg = (
                    f"📉 <b>Bearish Divergence CONFIRMED</b> 📉\n\n"
                    f"💎 Symbol: <b>{SYMBOLS[symbol]}</b>\n"
                    f"⏳ Timeframe: <b>{tf_name}</b>\n"
                    f"⏰ Time (Tehran): <b>{dt_tehran}</b>\n"
                    f"🌐 Session: <b>{session}</b>\n\n"
                    f"📊 <b>Histogram:</b>\n"
                    f"🔹 Top 1: <code>{t1_val:.6f}</code>\n"
                    f"🔹 Middle Trough: <code>{trough_val:.6f}</code>\n"
                    f"🔹 Top 2: <code>{t2_val:.6f}</code>\n\n"
                    f"💰 <b>Price (Shadow):</b>\n"
                    f"🔹 Top 1: <code>{df_closed['High'].iloc[t1_idx]:.6f}</code>\n"
                    f"🔹 Top 2: <code>{df_closed['High'].iloc[t2_idx]:.6f}</code>"
                )
                broadcast(msg)
                print(f"Signal Sent: {symbol} {tf_name} Bearish")
                return

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
        except Exception:
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
                except Exception:
                    time.sleep(10)
        
        bot_thread = threading.Thread(target=run_bot_polling)
        bot_thread.daemon = True
        bot_thread.start()
    
    print("Starting Flask web server...")
    run_web()
