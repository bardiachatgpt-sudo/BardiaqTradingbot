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

def get_symbol_menu():
    markup = types.InlineKeyboardMarkup(row_width=2)
    btns = []
    for sym, name in SYMBOLS.items():
        btns.append(types.InlineKeyboardButton(name, callback_data=f"check_{sym}"))
    markup.add(*btns)
    return markup

if bot:
    @bot.message_handler(commands=['start'])
    def send_welcome(message):
        subscribed_users.add(message.chat.id)
        bot.reply_to(
            message, 
            "✅ <b>Bot is Active.</b>\n\n"
            "Select a pair to see its current status:", 
            reply_markup=get_symbol_menu(),
            parse_mode="HTML"
        )

    @bot.callback_query_handler(func=lambda call: True)
    def handle_query(call):
        if call.data.startswith("check_"):
            sym = call.data.replace("check_", "")
            name = SYMBOLS[sym]
            
            bot.answer_callback_query(call.id, f"Fetching data for {name}...")
            
            msg = f"💎 <b>{name} Status:</b>\n\n"
            found_data = False
            
            for tf_code, tf_name in TIMEFRAMES.items():
                df = fetch_tradingview_candles(sym, tf_code, 220)
                if df is not None:
                    state_text = analyze_current_state(df)
                    msg += f"⏱ <b>{tf_name}</b>:\n{state_text}\n\n"
                    found_data = True
                else:
                    msg += f"⏱ <b>{tf_name}</b>: Failed to fetch data ⚠️\n\n"
            
            if not found_data:
                msg = f"❌ Could not fetch data for {name}. Please try again."
                
            bot.send_message(call.message.chat.id, msg, parse_mode="HTML", reply_markup=get_symbol_menu())

@app.route('/')
def home():
    return "Bot is LIVE!"

def run_web():
    port = int(os.environ.get("PORT", 7860))
    app.run(host="0.0.0.0", port=port)

# --- EXACT WEBSOCKET LOGIC FROM USER'S FIRST MESSAGE ---
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
# -----------------------------------------------------

def get_histogram(df):
    df_closed = df.iloc[:-1].copy()
    if len(df_closed) < 150: return None, None
    exp1 = df_closed['Close'].ewm(span=48, adjust=False).mean()
    exp2 = df_closed['Close'].ewm(span=104, adjust=False).mean()
    macd_line = exp1 - exp2
    signal_line = macd_line.ewm(span=36, adjust=False).mean()
    df_closed['Histogram'] = macd_line - signal_line
    return df_closed, df_closed['Histogram'].tolist()

def get_state(hist):
    last_hist = hist[-1]
    
    if last_hist < 0:
        # NEGATIVE PHASE
        phase_start = 0
        for i in range(len(hist)-1, -1, -1):
            if hist[i] > 0:
                phase_start = i
                break
                
        state = 0
        b1_val, b1_idx = 999999.0, -1
        peak_val, peak_idx = -999999.0, -1
        b2_val, b2_idx = 999999.0, -1
        up_count, down_count = 0, 0
        
        for i in range(phase_start, len(hist)):
            h_curr = hist[i]
            h_prev = hist[i-1] if i > phase_start else h_curr
            
            if state == 0:
                if h_curr < b1_val:
                    b1_val, b1_idx = h_curr, i
                if h_curr > h_prev: up_count += 1
                else: up_count = 0
                if up_count >= 7:
                    state = 1
                    peak_val, peak_idx = h_curr, i
                    down_count = 0
                    
            elif state == 1:
                if h_curr > peak_val:
                    peak_val, peak_idx = h_curr, i
                if h_curr < b1_val:
                    b1_val, b1_idx = h_curr, i
                    state = 0
                    up_count, down_count = 0, 0
                else:
                    if h_curr < h_prev: down_count += 1
                    else: down_count = 0
                    if down_count >= 3:
                        state = 2
                        b2_val, b2_idx = h_curr, i
                        up_count = 0
                        
            elif state == 2:
                if h_curr < b2_val:
                    b2_val, b2_idx = h_curr, i
                if h_curr > peak_val:
                    peak_val, peak_idx = h_curr, i
                    state = 1
                    down_count = 0
                elif h_curr < b1_val:
                    b1_val, b1_idx = h_curr, i
                    state = 0
                    up_count, down_count = 0, 0
                else:
                    if h_curr > h_prev: up_count += 1
                    else: up_count = 0
                    
        return "NEG", state, b1_val, b1_idx, peak_val, peak_idx, b2_val, b2_idx
        
    elif last_hist > 0:
        # POSITIVE PHASE
        phase_start = 0
        for i in range(len(hist)-1, -1, -1):
            if hist[i] < 0:
                phase_start = i
                break
                
        state = 0
        t1_val, t1_idx = -999999.0, -1
        trough_val, trough_idx = 999999.0, -1
        t2_val, t2_idx = -999999.0, -1
        down_count, up_count = 0, 0
        
        for i in range(phase_start, len(hist)):
            h_curr = hist[i]
            h_prev = hist[i-1] if i > phase_start else h_curr
            
            if state == 0:
                if h_curr > t1_val:
                    t1_val, t1_idx = h_curr, i
                if h_curr < h_prev: down_count += 1
                else: down_count = 0
                if down_count >= 7:
                    state = 1
                    trough_val, trough_idx = h_curr, i
                    up_count = 0
                    
            elif state == 1:
                if h_curr < trough_val:
                    trough_val, trough_idx = h_curr, i
                if h_curr > t1_val:
                    t1_val, t1_idx = h_curr, i
                    state = 0
                    down_count, up_count = 0, 0
                else:
                    if h_curr > h_prev: up_count += 1
                    else: up_count = 0
                    if up_count >= 3:
                        state = 2
                        t2_val, t2_idx = h_curr, i
                        down_count = 0
                        
            elif state == 2:
                if h_curr > t2_val:
                    t2_val, t2_idx = h_curr, i
                if h_curr < trough_val:
                    trough_val, trough_idx = h_curr, i
                    state = 1
                    up_count = 0
                elif h_curr > t1_val:
                    t1_val, t1_idx = h_curr, i
                    state = 0
                    down_count, up_count = 0, 0
                else:
                    if h_curr < h_prev: down_count += 1
                    else: down_count = 0
                    
        return "POS", state, t1_val, t1_idx, trough_val, trough_idx, t2_val, t2_idx
        
    return None

def analyze_current_state(df):
    df_closed, hist = get_histogram(df)
    if df_closed is None: return "Not enough data."
    
    res = get_state(hist)
    if not res: return "Phase unclear."
    
    phase, state, ext1_val, ext1_idx, mid_val, mid_idx, ext2_val, ext2_idx = res
    
    if phase == "NEG":
        if state == 0:
            return f"🔴 <b>Phase: Negative</b>\nStill dropping. Bottom 1 forming at {df_closed['Low'].iloc[ext1_idx]:.5f} (Abs: {abs(ext1_val):.6f}).\nWaiting for retracement..."
        elif state == 1:
            return f"🔴 <b>Phase: Negative</b>\nBottom 1: {df_closed['Low'].iloc[ext1_idx]:.5f} (Abs: {abs(ext1_val):.6f})\nMiddle Peak forming at {df_closed['Low'].iloc[mid_idx]:.5f} (Abs: {abs(mid_val):.6f}).\nWaiting for pullback..."
        else:
            return f"🔴 <b>Phase: Negative</b>\nBottom 1: {df_closed['Low'].iloc[ext1_idx]:.5f} (Abs: {abs(ext1_val):.6f})\nMiddle Peak: {df_closed['Low'].iloc[mid_idx]:.5f} (Abs: {abs(mid_val):.6f})\nBottom 2 forming at {df_closed['Low'].iloc[ext2_idx]:.5f} (Abs: {abs(ext2_val):.6f}).\nWatching for confirmation..."
    else:
        if state == 0:
            return f"🟢 <b>Phase: Positive</b>\nStill rising. Top 1 forming at {df_closed['High'].iloc[ext1_idx]:.5f} (Val: {ext1_val:.6f}).\nWaiting for pullback..."
        elif state == 1:
            return f"🟢 <b>Phase: Positive</b>\nTop 1: {df_closed['High'].iloc[ext1_idx]:.5f} (Val: {ext1_val:.6f})\nMiddle Trough forming at {df_closed['High'].iloc[mid_idx]:.5f} (Val: {mid_val:.6f}).\nWaiting for retracement..."
        else:
            return f"🟢 <b>Phase: Positive</b>\nTop 1: {df_closed['High'].iloc[ext1_idx]:.5f} (Val: {ext1_val:.6f})\nMiddle Trough: {df_closed['High'].iloc[mid_idx]:.5f} (Val: {mid_val:.6f})\nTop 2 forming at {df_closed['High'].iloc[ext2_idx]:.5f} (Val: {ext2_val:.6f}).\nWatching for confirmation..."

def check_divergence(df, symbol, tf_name):
    df_closed, hist = get_histogram(df)
    if df_closed is None or len(hist) < 150: return

    res = get_state(hist)
    if not res: return
    
    phase, state, ext1_val, ext1_idx, mid_val, mid_idx, ext2_val, ext2_idx = res
    
    if phase == "NEG":
        if state == 2 and ext2_idx == len(hist) - 2:
            if (ext2_idx - ext1_idx) >= 30 and abs(ext1_val) > abs(ext2_val) and df_closed['Low'].iloc[ext1_idx] > df_closed['Low'].iloc[ext2_idx]:
                dt_tehran = df_closed.index[ext2_idx].astimezone(tehran_tz).strftime('%H:%M')
                session = get_current_session()
                msg = (
                    f"🚀 <b>Bullish Divergence CONFIRMED</b> 🚀\n\n"
                    f"💎 Symbol: <b>{SYMBOLS[symbol]}</b>\n"
                    f"⏳ Timeframe: <b>{tf_name}</b>\n"
                    f"⏰ Time (Tehran): <b>{dt_tehran}</b>\n"
                    f"🌐 Session: <b>{session}</b>\n\n"
                    f"📊 <b>Abs Histogram:</b>\n"
                    f"🔹 Bottom 1: <code>{abs(ext1_val):.6f}</code>\n"
                    f"🔹 Middle Peak: <code>{abs(mid_val):.6f}</code>\n"
                    f"🔹 Bottom 2: <code>{abs(ext2_val):.6f}</code>\n\n"
                    f"💰 <b>Price (Shadow):</b>\n"
                    f"🔹 Bottom 1: <code>{df_closed['Low'].iloc[ext1_idx]:.6f}</code>\n"
                    f"🔹 Bottom 2: <code>{df_closed['Low'].iloc[ext2_idx]:.6f}</code>"
                )
                broadcast(msg)
                print(f"Signal Sent: {symbol} {tf_name} Bullish")
                return

    elif phase == "POS":
        if state == 2 and ext2_idx == len(hist) - 2:
            if (ext2_idx - ext1_idx) >= 30 and ext1_val > ext2_val and df_closed['High'].iloc[ext1_idx] < df_closed['High'].iloc[ext2_idx]:
                dt_tehran = df_closed.index[ext2_idx].astimezone(tehran_tz).strftime('%H:%M')
                session = get_current_session()
                msg = (
                    f"📉 <b>Bearish Divergence CONFIRMED</b> 📉\n\n"
                    f"💎 Symbol: <b>{SYMBOLS[symbol]}</b>\n"
                    f"⏳ Timeframe: <b>{tf_name}</b>\n"
                    f"⏰ Time (Tehran): <b>{dt_tehran}</b>\n"
                    f"🌐 Session: <b>{session}</b>\n\n"
                    f"📊 <b>Histogram:</b>\n"
                    f"🔹 Top 1: <code>{ext1_val:.6f}</code>\n"
                    f"🔹 Middle Trough: <code>{mid_val:.6f}</code>\n"
                    f"🔹 Top 2: <code>{ext2_val:.6f}</code>\n\n"
                    f"💰 <b>Price (Shadow):</b>\n"
                    f"🔹 Top 1: <code>{df_closed['High'].iloc[ext1_idx]:.6f}</code>\n"
                    f"🔹 Top 2: <code>{df_closed['High'].iloc[ext2_idx]:.6f}</code>"
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
                    time.sleep(2) 
            time.sleep(10) 
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
