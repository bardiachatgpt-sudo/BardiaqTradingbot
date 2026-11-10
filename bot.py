import os
import time
import json
import logging
import threading
import requests
import numpy as np
import pandas as pd
from flask import Flask

# خواندن اطلاعات امن از رندر (Environment Variables)
OANDA_API_KEY = os.environ.get("OANDA_API_KEY")
OANDA_ACCOUNT_ID = os.environ.get("OANDA_ACCOUNT_ID")
OANDA_ENVIRONMENT = os.environ.get("OANDA_ENVIRONMENT", "practice")

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

SYMBOLS = [
    "EUR_USD", "GBP_USD", "USD_JPY", "AUD_USD", 
    "USD_CAD", "NZD_USD", "USD_CHF", "XAU_USD"
]

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
sent_macro_alerts = {}

# وب‌سرور ساده برای روشن ماندن ۲۴ ساعته در رندر
app = Flask(__name__)

@app.route('/')
def home():
    return "Trading Bot is running!"

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)


# دریافت داده از اواندا
class OandaDataLoader:
    def __init__(self):
        if OANDA_ENVIRONMENT == "live":
            self.base_url = "https://api-fxtrade.oanda.com/v3"
        else:
            self.base_url = "https://api-fxpractice.oanda.com/v3"
            
        self.headers = {
            "Authorization": f"Bearer {OANDA_API_KEY}",
            "Accept-DatetimeFormat": "RFC3339"
        }

    def fetch_candles(self, instrument: str, granularity: str, count: int = 300) -> pd.DataFrame:
        url = f"{self.base_url}/instruments/{instrument}/candles"
        params = {"granularity": granularity, "count": count, "price": "M"}
        try:
            response = requests.get(url, headers=self.headers, params=params, timeout=10)
            if response.status_code != 200:
                return pd.DataFrame()
            data = response.json()
            candles = data.get("candles", [])
            parsed_data = []
            for c in candles:
                if c.get("complete", False):
                    mid = c["mid"]
                    parsed_data.append({
                        "time": c["time"],
                        "open": float(mid["o"]),
                        "high": float(mid["h"]),
                        "low": float(mid["l"]),
                        "close": float(mid["c"]),
                        "volume": int(c["volume"])
                    })
            df = pd.DataFrame(parsed_data)
            if not df.empty:
                df["time"] = pd.to_datetime(df["time"])
                df.set_index("time", inplace=True)
            return df
        except Exception as e:
            logging.error(f"خطا در ارتباط با اواندا برای {instrument}: {e}")
            return pd.DataFrame()


# اندیکاتورها و پیوت‌ها
def calculate_macd(df: pd.DataFrame, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    df = df.copy()
    ema_fast = df['close'].ewm(span=fast, adjust=False).mean()
    ema_slow = df['close'].ewm(span=slow, adjust=False).mean()
    df['macd_line'] = ema_fast - ema_slow
    df['signal_line'] = df['macd_line'].ewm(span=signal, adjust=False).mean()
    df['histogram'] = df['macd_line'] - df['signal_line']
    return df

def find_pivots(df: pd.DataFrame, order: int = 5) -> pd.DataFrame:
    df = df.copy()
    df['pivot_high'] = np.nan
    df['pivot_low'] = np.nan
    highs = df['high'].values
    lows = df['low'].values
    for i in range(order, len(df) - order):
        if highs[i] == max(highs[i - order:i + order + 1]):
            df.iloc[i, df.columns.get_loc('pivot_high')] = highs[i]
        if lows[i] == min(lows[i - order:i + order + 1]):
            df.iloc[i, df.columns.get_loc('pivot_low')] = lows[i]
    return df

def detect_divergence(df: pd.DataFrame) -> dict:
    result = {"bullish_div": False, "bearish_div": False}
    if len(df) < 20:
        return result
    recent_lows = df[df['pivot_low'].notna()].tail(2)
    recent_highs = df[df['pivot_high'].notna()].tail(2)
    if len(recent_lows) == 2:
        p1, p2 = recent_lows.iloc[0], recent_lows.iloc[1]
        if p2['close'] < p1['close'] and p2['macd_line'] > p1['macd_line']:
            result["bullish_div"] = True
    if len(recent_highs) == 2:
        h1, h2 = recent_highs.iloc[0], recent_highs.iloc[1]
        if h2['close'] > h1['close'] and h2['macd_line'] < h1['macd_line']:
            result["bearish_div"] = True
    return result

def check_al_brooks_signal_1m(df: pd.DataFrame, direction: str) -> bool:
    if len(df) < 3:
        return False
    current_bar = df.iloc[-1]
    prev_bar = df.iloc[-2]
    body = abs(current_bar['close'] - current_bar['open'])
    candle_range = current_bar['high'] - current_bar['low']
    if candle_range == 0:
        return False
    body_ratio = body / candle_range
    if direction == 'BUY':
        is_bullish = current_bar['close'] > current_bar['open']
        strong_close = (current_bar['close'] >= (current_bar['high'] - candle_range * 0.35))
        breakout_high = current_bar['close'] > prev_bar['high']
        if is_bullish and (strong_close or breakout_high) and body_ratio >= 0.3:
            return True
    elif direction == 'SELL':
        is_bearish = current_bar['close'] < current_bar['open']
        strong_close = (current_bar['close'] <= (current_bar['low'] + candle_range * 0.35))
        breakout_low = current_bar['close'] < prev_bar['low']
        if is_bearish and (strong_close or breakout_low) and body_ratio >= 0.3:
            return True
    return False


# بخش ۱: دایورجنس ۵ دقیقه‌ای (مکدی ۴۸-۱۰۶-۳۶)
def scan_m5_divergence(symbol: str) -> list:
    loader = OandaDataLoader()
    alerts = []
    df = loader.fetch_candles(symbol, "M5", count=250)
    if df.empty or len(df) < 130:
        return alerts
    df = calculate_macd(df, fast=48, slow=106, signal=36)
    df = find_pivots(df, order=5)
    div = detect_divergence(df)
    current_price = df.iloc[-1]['close']
    if div["bullish_div"]:
        alerts.append({"type": "BULLISH", "text": "🟢 دایورجنس صعودی M5 (48-106-36)", "price": current_price})
    elif div["bearish_div"]:
        alerts.append({"type": "BEARISH", "text": "🔴 دایورجنس نزولی M5 (48-106-36)", "price": current_price})
    return alerts


# بخش ۲: استراتژی اصلی
def evaluate_symbol_strategy(symbol: str) -> dict:
    loader = OandaDataLoader()
    df_daily = loader.fetch_candles(symbol, "D", count=100)
    df_h4 = loader.fetch_candles(symbol, "H4", count=100)
    df_h1 = loader.fetch_candles(symbol, "H1", count=50)
    df_m5 = loader.fetch_candles(symbol, "M5", count=50)
    df_m1 = loader.fetch_candles(symbol, "M1", count=50)
    if df_daily.empty or df_h4.empty or df_h1.empty or df_m5.empty or df_m1.empty:
        return {"signal": None}
    df_daily = calculate_macd(df_daily, fast=3, slow=6, signal=2)
    df_daily = find_pivots(df_daily, order=3)
    df_h4 = calculate_macd(df_h4, fast=12, slow=26, signal=9)
    df_h4 = find_pivots(df_h4, order=5)
    h4_div = detect_divergence(df_h4)
    scenario = "Continuation"
    locked_direction = "BUY"
    if h4_div["bearish_div"]:
        scenario = "Reversal"
        locked_direction = "SELL"
    elif h4_div["bullish_div"]:
        scenario = "Reversal"
        locked_direction = "BUY"
    df_h1 = calculate_macd(df_h1, fast=12, slow=26, signal=9)
    if locked_direction == "BUY" and df_h1.iloc[-1]['histogram'] < 0: return {"signal": None}
    if locked_direction == "SELL" and df_h1.iloc[-1]['histogram'] > 0: return {"signal": None}
    df_m5 = calculate_macd(df_m5, fast=6, slow=13, signal=5)
    if locked_direction == "BUY" and df_m5.iloc[-1]['histogram'] < 0: return {"signal": None}
    if locked_direction == "SELL" and df_m5.iloc[-1]['histogram'] > 0: return {"signal": None}
    df_m1_calc = calculate_macd(df_m1, fast=3, slow=6, signal=2)
    df_m1_calc = find_pivots(df_m1_calc, order=2)
    if check_al_brooks_signal_1m(df_m1_calc, locked_direction):
        return {"signal": locked_direction, "scenario": scenario, "price": df_m1.iloc[-1]['close']}
    return {"signal": None}


# بخش ۳: آخرین سیگنال قطعی ورود (تاریخچه)
def find_last_confirmed_signal(symbol: str) -> str:
    loader = OandaDataLoader()
    df_m1 = loader.fetch_candles(symbol, "M1", count=100)
    if df_m1.empty:
        return "اطلاعاتی یافت نشد."
    for i in range(len(df_m1) - 1, 10, -1):
        sub_m1 = df_m1.iloc[:i+1]
        is_buy = check_al_brooks_signal_1m(sub_m1, 'BUY')
        is_sell = check_al_brooks_signal_1m(sub_m1, 'SELL')
        if is_buy or is_sell:
            return f"زمان: `{sub_m1.index[-1].strftime('%Y-%m-%d %H:%M')}` | جهت: {'🟢 خرید' if is_buy else '🔴 فروش'} | قیمت: `{sub_m1.iloc[-1]['close']}`"
    return "در بازه اخیر سیگنالی ثبت نشده است."


# دکمه‌های تلگرام
def send_telegram_message(chat_id, text, reply_markup=None):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        logging.error(f"خطا در ارسال پیام تلگرام: {e}")

def get_main_keyboard():
    return {
        "keyboard": [
            [{"text": "📊 استراتژی اصلی"}, {"text": "📡 دایورجنس M5"}],
            [{"text": "📜 آخرین سیگنال‌ها"}, {"text": "🔍 اسکن جامع بازار"}]
        ],
        "resize_keyboard": True
    }


# شنود دکمه‌ها در تلگرام
def telegram_listener():
    logging.info("بخش تعاملی تلگرام فعال شد...")
    offset = 0
    while True:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
            params = {"offset": offset, "timeout": 30}
            response = requests.get(url, params=params, timeout=35)
            if response.status_code == 200:
                for update in response.json().get("result", []):
                    offset = update["update_id"] + 1
                    message = update.get("message", {})
                    chat_id = message.get("chat", {}).get("id")
                    text = message.get("text", "").strip()
                    if not chat_id or not text:
                        continue
                    
                    if text in ["/start", "شروع"]:
                        send_telegram_message(chat_id, "👋 سلام! پنل معاملاتی شما آماده است. از دکمه‌های زیر استفاده کن 👇", reply_markup=get_main_keyboard())
                        
                    elif text in ["📡 دایورجنس M5", "/m5_div"]:
                        send_telegram_message(chat_id, "🔍 در حال اسکن دایورجنس‌های ۵ دقیقه‌ای (48-106-36)...")
                        found = False
                        for symbol in SYMBOLS:
                            for al in scan_m5_divergence(symbol):
                                found = True
                                send_telegram_message(chat_id, f"📡 **[M5 Div]** `{symbol}`\n{al['text']}\nقیمت: `{al['price']}`")
                        if not found:
                            send_telegram_message(chat_id, "📊 هیچ دایورجنس ۵ دقیقه‌ای فعالی یافت نشد.")
                            
                    elif text in ["📊 استراتژی اصلی", "/strategy"]:
                        send_telegram_message(chat_id, "🔍 در حال بررسی استراتژی چندتایم‌فریمی...")
                        found = False
                        for symbol in SYMBOLS:
                            res = evaluate_symbol_strategy(symbol)
                            if res.get("signal"):
                                found = True
                                send_telegram_message(chat_id, f"🚨 **[استراتژی اصلی]** `{symbol}`\nجهت: {'🟢 BUY' if res['signal']=='BUY' else '🔴 SELL'}\nسناریو: `{res['scenario']}`\nقیمت: `{res['price']}`")
                        if not found:
                            send_telegram_message(chat_id, "📊 فعلاً هیچ نمادی شرایط کامل استراتژی اصلی را ندارد.")
                            
                    elif text in ["📜 آخرین سیگنال‌ها", "/history"]:
                        send_telegram_message(chat_id, "⏳ در حال استخراج آخرین سیگنال‌های قطعی ورود...")
                        report = "📜 **آخرین سیگنال ثبت‌شده در هر نماد:**\n\n"
                        for symbol in SYMBOLS:
                            report += f"🔹 **{symbol}**:\n{find_last_confirmed_signal(symbol)}\n\n"
                        send_telegram_message(chat_id, report)
                        
                    elif text in ["🔍 اسکن جامع بازار", "/scan"]:
                        send_telegram_message(chat_id, "🚀 اسکن جامع آغاز شد...", reply_markup=get_main_keyboard())
                        for symbol in SYMBOLS:
                            for al in scan_m5_divergence(symbol):
                                send_telegram_message(chat_id, f"📡 **[M5 Div]** `{symbol}`\n{al['text']}")
                            res = evaluate_symbol_strategy(symbol)
                            if res.get("signal"):
                                send_telegram_message(chat_id, f"🚨 **[استراتژی اصلی]** `{symbol}` -> {res['signal']}")
                        send_telegram_message(chat_id, "✅ اسکن جامع تمام شد.")
        except Exception as e:
            logging.error(f"خطا در تلگرام: {e}")
            time.sleep(5)


# اسکنر خودکار پس‌زمینه
def background_auto_scanner():
    while True:
        for symbol in SYMBOLS:
            try:
                for al in scan_m5_divergence(symbol):
                    key = f"{symbol}_M5_{al['type']}"
                    if sent_macro_alerts.get(key) != "sent":
                        sent_macro_alerts[key] = "sent"
                        send_telegram_message(TELEGRAM_CHAT_ID, f"🚨 **هشدار خودکار M5**\nنماد: `{symbol}`\n{al['text']}\nقیمت: `{al['price']}`", reply_markup=get_main_keyboard())
                
                strat = evaluate_symbol_strategy(symbol)
                if strat.get("signal"):
                    send_telegram_message(TELEGRAM_CHAT_ID, f"🚨 **سیگنال قطعی ورود (خودکار)**\nنماد: `{symbol}`\nجهت: {strat['signal']}\nقیمت: `{strat['price']}`", reply_markup=get_main_keyboard())
                    time.sleep(10)
            except Exception as e:
                logging.error(f"خطا در اسکن خودکار {symbol}: {e}")
        time.sleep(60)


if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()
    threading.Thread(target=telegram_listener, daemon=True).start()
    background_auto_scanner()
