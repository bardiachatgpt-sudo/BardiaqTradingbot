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
                    "p1_price": float(data.get("p1_price", 0)),
                    "macd": float(data.get("macd", 0)),
                    "p1_macd": float(data.get("p1_macd", 0)),
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

SYMBOLS = {
    "OANDA:XAUUSD": "XAUUSD (Gold)",
    "FX:EURUSD": "EURUSD",
    "FX:GBPUSD": "GBPUSD",
    "FX:USDJPY": "USDJPY",
    "FX:AUDUSD": "AUDUSD",
    "FX:USDCAD": "USDCAD",
    "FX:USDCHF": "USDCHF",
    "FX:NZDUSD": "NZDUSD"
}

TIMEFRAMES = {"1": "M1", "5": "M5", "15": "M15", "60": "H1", "240": "H4"}

def get_main_menu():
    markup = types.InlineKeyboardMarkup(row_width=2)
    btn_live = types.InlineKeyboardButton("Live Radar", callback_data="menu_live")
    btn_history = types.InlineKeyboardButton("History (220 Candles)", callback_data="menu_history")
    markup.row(btn_live, btn_history)
    btns = []
    for sym, name in SYMBOLS.items():
        btns.append(types.InlineKeyboardButton(name, callback_data=f"sym_{sym}"))
    markup.add(*btns)
    return markup

if bot:
    @bot.message_handler(commands=['start'])
    def send_welcome(message):
        subscribed_users.add(message.chat.id)
        save_state()
        bot.reply_to(
            message, 
            "Bot is active.\n\n"
            "Strategy: MACD Line (48, 104, 36) Divergence.\n"
            "Choose an option below:", 
            reply_markup=get_main_menu()
        )

    @bot.callback_query_handler(func=lambda call: True)
    def handle_query(call):
        bot.answer_callback_query(call.id, "Processing...")
        
        if call.data == "menu_live":
            msg = "<b>Live Divergences (Last 15 mins):</b>\n\n"
            found = False
            for sym, name in SYMBOLS.items():
                for tf_code in ["1", "5", "15"]:
                    tf_name = TIMEFRAMES[tf_code]
                    df, err = fetch_tradingview_candles(sym, tf_code, 220)
                    if df is not None:
                        signals = detect_macd_divergence(df)
                        if signals:
                            latest = signals[-1]
                            if latest['bars_ago'] <= 3: 
                                dt_tehran = latest["time"].astimezone(tehran_tz).strftime('%H:%M')
                                sig_type = "Bullish 🟢" if latest['type'] == 'Bullish' else "Bearish 🔴"
                                msg += f"<b>{name}</b> | {tf_name}: {sig_type} | {dt_tehran}\n"
                                found = True
            if not found:
                msg += "No live signals found in the last 15 mins. ❌"
            bot.send_message(call.message.chat.id, msg, parse_mode="HTML", reply_markup=get_main_menu())
            
        elif call.data == "menu_history":
            bot.send_message(call.message.chat.id, "Select a pair from the menu below:", reply_markup=get_main_menu())
            
        elif call.data.startswith("sym_"):
            sym = call.data.replace("sym_", "")
            name = SYMBOLS[sym]
            msg = f"<b>Last Divergences for {name} (220 Candles):</b>\n\n"
            found_any = False
            for tf_code, tf_name in TIMEFRAMES.items():
                df, err = fetch_tradingview_candles(sym, tf_code, 220)
                if df is not None:
                    signals = detect_macd_divergence(df)
                    if signals:
                        latest = signals[-1]
                        dt_tehran = latest["time"].astimezone(tehran_tz).strftime('%Y-%m-%d %H:%M')
                        sig_type = "Bullish 🟢" if latest['type'] == 'Bullish' else "Bearish 🔴"
                        bars_ago = latest.get('bars_ago', 0)
                        msg += f"<b>{tf_name}</b>: {sig_type} | {dt_tehran} | ({bars_ago} bars ago)\n"
                        found_any = True
                    else:
                        msg += f"<b>{tf_name}</b>: No Signal ❌\n"
                else:
                    msg += f"<b>{tf_name}</b>: Data Error ⚠️\n"
            if not found_any:
                msg += "\nNo valid divergences found."
            bot.send_message(call.message.chat.id, msg, parse_mode="HTML", reply_markup=get_main_menu())

@app.route('/')
def home():
    return "MACD Bot is LIVE!"

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

def detect_macd_divergence(df):
    if df is None or len(df) < 150:
        return []

    df = df.iloc[:-1]
    if len(df) < 150:
        return []

    exp1 = df['Close'].ewm(span=48, adjust=False).mean()
    exp2 = df['Close'].ewm(span=104, adjust=False).mean()
    macd_line = exp1 - exp2
    
    signals = []
    total_bars = len(df)
    
    bottoms = []
    tops = []
    for i in range(2, len(macd_line) - 1):
        if macd_line.iloc[i] < macd_line.iloc[i-1] and macd_line.iloc[i] < macd_line.iloc[i+1]:
            if macd_line.iloc[i] < 0:
                bottoms.append(i)
        if macd_line.iloc[i] > macd_line.iloc[i-1] and macd_line.iloc[i] > macd_line.iloc[i+1]:
            if macd_line.iloc[i] > 0:
                tops.append(i)

    for i in range(len(bottoms) - 1):
        b1 = bottoms[i]
        b2 = bottoms[i+1]
        if (macd_line.iloc[b1:b2+1] < 0).all():
            m1 = macd_line.iloc[b1]
            m2 = macd_line.iloc[b2]
            low1 = df['Low'].iloc[b1]
            low2 = df['Low'].iloc[b2]
            if abs(m1) > abs(m2) and low1 > low2:
                bars_ago = total_bars - 1 - b2
                signals.append({
                    "type": "Bullish",
                    "price": low2,
                    "p1_price": low1,
                    "macd": m2,
                    "p1_macd": m1,
                    "time": df.index[b2],
                    "bars_ago": bars_ago
                })

    for i in range(len(tops) - 1):
        t1 = tops[i]
        t2 = tops[i+1]
        if (macd_line.iloc[t1:t2+1] > 0).all():
            m1 = macd_line.iloc[t1]
            m2 = macd_line.iloc[t2]
            high1 = df['High'].iloc[t1]
            high2 = df['High'].iloc[t2]
            if m1 > m2 and high1 < high2:
                bars_ago = total_bars - 1 - t2
                signals.append({
                    "type": "Bearish",
                    "price": high2,
                    "p1_price": high1,
                    "macd": m2,
                    "p1_macd": m1,
                    "time": df.index[t2],
                    "bars_ago": bars_ago
                })

    if signals:
        return [signals[-1]]
    return []

live_notifications_cache = {}

def live_scanner():
    while True:
        try:
            if not bot or not subscribed_users:
                time.sleep(10)
                continue
                
            for symbol, persian_name in SYMBOLS.items():
                for tf_code, tf_name in TIMEFRAMES.items():
                    df, err = fetch_tradingview_candles(symbol, tf_code, 220)
                    if df is None: continue
                    
                    signals = detect_macd_divergence(df)
                    if signals:
                        latest_sig = signals[-1]
                        sig_time = latest_sig["time"]
                        key = f"{symbol}_{tf_name}"
                        
                        if latest_sig['bars_ago'] == 0: 
                            if live_notifications_cache.get(key) != sig_time:
                                live_notifications_cache[key] = sig_time
                                last_divergences[key] = latest_sig
                                save_state()
                                
                                dt_tehran = sig_time.astimezone(tehran_tz).strftime('%H:%M')
                                icon = "🚀" if latest_sig['type'] == "Bullish" else "📉"
                                title = "Bullish Divergence" if latest_sig['type'] == "Bullish" else "Bearish Divergence"
                                
                                msg = (
                                    f"{icon} <b>{title} MACD</b>\n\n"
                                    f"Symbol: <b>{persian_name}</b>\n"
                                    f"Timeframe: <b>{tf_name}</b>\n"
                                    f"Time (Tehran): <b>{dt_tehran}</b>\n\n"
                                    f"MACD 1: {latest_sig['p1_macd']:.5f}\n"
                                    f"MACD 2: {latest_sig['macd']:.5f}"
                                )
                                broadcast(msg)
                    time.sleep(0.5) 
            time.sleep(10) 
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
                    print("Starting Telegram polling...")
                    bot.infinity_polling(timeout=20, long_polling_timeout=15)
                except Exception as e:
                    print(f"Polling Error: {e}")
                    time.sleep(10)
        
        bot_thread = threading.Thread(target=run_bot_polling)
        bot_thread.daemon = True
        bot_thread.start()
    
    print("Starting Flask web server...")
    run_web()
