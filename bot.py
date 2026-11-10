import time
import json
import logging
import threading
import requests
import numpy as np
import pandas as pd
from flask import Flask

# ==================== 🔑 اطلاعات خودت را همینجا مستقیم وارد کن ====================
OANDA_API_KEY = "توکن_اواندا_اینجا"
OANDA_ACCOUNT_ID = "آی_دی_حساب_اواندا_اینجا"
OANDA_ENVIRONMENT = "practice"  # اگر اکانت اصلی است بنویس live

TELEGRAM_BOT_TOKEN = "توکن_ربات_تلگرام_اینجا"
TELEGRAM_CHAT_ID = "چت_آیدی_عددی_تلگرام_خودت_اینجا"
# ================================================================================

SYMBOLS = [
    "EUR_USD", "GBP_USD", "USD_JPY", "AUD_USD", 
    "USD_CAD", "NZD_USD", "USD_CHF", "XAU_USD"
]

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
sent_macro_alerts = {}

# وب‌سرور ساده برای روشن ماندن ۲۴ ساعته در رندر (با کمک سایت Cron)
app = Flask(__name__)

@app.route('/')
def home():
    return "Autonomous Trading Bot is running!"

def run_flask():
    app.run(host='0.0.0.0', port=10000)


# ==================== 📊 دریافت خودکار داده از اواندا ====================
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


# ==================== 📈 محاسبه اندیکاتورها و دایورجنس ====================
def calculate_macd(df: pd.DataFrame, fast: int = 48, slow: int = 106, signal: int = 36) -> pd.DataFrame:
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
    if len(df) < 30:
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


# ==================== 🔍 اسکنر خودکار دایورجنس M5 (مکدی ۴۸-۱۰۶-۳۶) ====================
def scan_m5_divergence(symbol: str) -> list:
    loader = OandaDataLoader()
    alerts = []
    df = loader.fetch_candles(symbol, "M5", count=250)
    if df.empty or len(df) < 130:
        return alerts
    
    # اعمال تنظیمات مکدی درخواستی شما
    df = calculate_macd(df, fast=48, slow=106, signal=36)
    df = find_pivots(df, order=5)
    div = detect_divergence(df)
    current_price = df.iloc[-1]['close']
    
    if div["bullish_div"]:
        alerts.append({"type": "BULLISH", "text": "🟢 دایورجنس صعودی M5 (مکدی 48-106-36)", "price": current_price})
    elif div["bearish_div"]:
        alerts.append({"type": "BEARISH", "text": "🔴 دایورجنس نزولی M5 (مکدی 48-106-36)", "price": current_price})
    return alerts


# ==================== 🤖 ارسال پیام به تلگرام ====================
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
            [{"text": "📊 وضعیت ربات"}, {"text": "🔍 اسکن آنی بازار"}]
        ],
        "resize_keyboard": True
    }


# ==================== 💬 بخش تعاملی تلگرام (دکمه‌ها) ====================
def telegram_listener():
    logging.info("بخش شنود تلگرام فعال شد...")
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
                        send_telegram_message(chat_id, "👋 سلام! ربات خودمختار شما روشن است و بازار را رصد می‌کند.", reply_markup=get_main_keyboard())
                    elif text == "📊 وضعیت ربات":
                        send_telegram_message(chat_id, "✅ ربات بدون واسطه متصل به اوانداست و در پس‌زمینه بازار را اسکن می‌کند.")
                    elif text == "🔍 اسکن آنی بازار":
                        send_telegram_message(chat_id, "🔍 در حال اسکن تمام نمادها برای یافتن دایورجنس M5...")
                        found = False
                        for symbol in SYMBOLS:
                            for al in scan_m5_divergence(symbol):
                                found = True
                                send_telegram_message(chat_id, f"📡 **[بررسی دستی]** `{symbol}`\n{al['text']}\nقیمت: `{al['price']}`")
                        if not found:
                            send_telegram_message(chat_id, "📊 در حال حاضر هیچ دایورجنس فعالی در M5 یافت نشد.")
        except Exception as e:
            logging.error(f"خطا در تلگرام: {e}")
            time.sleep(5)


# ==================== 🔄 اسکنر خودکار پس‌زمینه (ارسال اتوماتیک پیام) ====================
def background_auto_scanner():
    global sent_macro_alerts
    logging.info("اسکنر خودکار پشت صحنه روشن شد...")
    while True:
        for symbol in SYMBOLS:
            try:
                # اسکن اتوماتیک دایورجنس M5
                m5_alerts = scan_m5_divergence(symbol)
                for al in m5_alerts:
                    key = f"{symbol}_M5_{al['type']}"
                    # ارسال پیام خودکار فقط وقتی دایورجنس جدید باشد تا اسپم نشود
                    if sent_macro_alerts.get(key) != "sent":
                        sent_macro_alerts[key] = "sent"
                        msg = (
                            f"🚨 **سیگنال خودکار دایورجنس** 🚨\n\n"
                            f"🔹 **نماد:** `{symbol}`\n"
                            f"{al['text']}\n"
                            f"💵 **قیمت لحظه‌ای:** `{al['price']}`"
                        )
                        send_telegram_message(TELEGRAM_CHAT_ID, msg, reply_markup=get_main_keyboard())
                
                # پاکسازی حافظه اگر دایورجنس تمام شده باشد
                active_keys = [f"{symbol}_M5_{al['type']}" for al in m5_alerts]
                for k in list(sent_macro_alerts.keys()):
                    if k.startswith(symbol) and k not in active_keys:
                        del sent_macro_alerts[k]

            except Exception as e:
                logging.error(f"خطا در اسکن خودکار {symbol}: {e}")
                
        time.sleep(60)  # بررسی مداوم هر ۶۰ ثانیه


# ==================== 🚀 اجرای کل سیستم ====================
if __name__ == "__main__":
    # ۱. راه‌اندازی وب‌سرور برای روشن ماندن در رندر
    threading.Thread(target=run_flask, daemon=True).start()
    
    # ۲. راه‌اندازی شنود دکمه‌های تلگرام
    threading.Thread(target=telegram_listener, daemon=True).start()
    
    # ۳. اجرای اسکنر خودکار پس‌زمینه (که خودش اتوماتیک پیام می‌دهد)
    background_auto_scanner()
