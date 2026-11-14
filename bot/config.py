"""تنظیمات عمومی و تعریف نمادها."""
import pytz

TEHRAN = pytz.timezone("Asia/Tehran")
PRICE_WINDOW = 7        # تعداد کندل قبل از کندل کف/قله برای قیمت (قابل‌تنظیم)
READY_BARS = 3          # حداقل تعداد میله‌ی پیاپی (12,26,9) در سمت درست برای READY (قابل‌تنظیم)
WARMUP = 1000           # کندل‌های گرم‌شدن اندیکاتور (بدون تشخیص)
INIT_BARS = 2500        # تاریخچه‌ی بازپخش هنگام راه‌اندازی
POLL_BARS = 60          # تعداد کندل در هر دور بررسی زنده
STATE_FILE = "bot_state.json"
WORKERS = 5             # تعداد نمادهایی که هم‌زمان دریافت می‌شوند


def _chain(base):
    return [f"OANDA:{base}", f"FOREXCOM:{base}", f"FX:{base}"]


# نماد → فهرست منابع TradingView به ترتیب اولویت
SYMBOLS = {
    "XAUUSD": ["OANDA:XAUUSD", "FOREXCOM:XAUUSD", "FX_IDC:XAUUSD"],
    "EURUSD": _chain("EURUSD"),
    "GBPUSD": _chain("GBPUSD"),
    "USDJPY": _chain("USDJPY"),
    "USDCHF": _chain("USDCHF"),
    "USDCAD": _chain("USDCAD"),
    "AUDUSD": _chain("AUDUSD"),
    "NZDUSD": _chain("NZDUSD"),
    "BTCUSD": ["BINANCE:BTCUSDT", "COINBASE:BTCUSD"],
}

FLAGS = {
    "XAUUSD": "🥇🇺🇸",
    "EURUSD": "🇪🇺🇺🇸",
    "GBPUSD": "🇬🇧🇺🇸",
    "USDJPY": "🇺🇸🇯🇵",
    "USDCHF": "🇺🇸🇨🇭",
    "USDCAD": "🇺🇸🇨🇦",
    "AUDUSD": "🇦🇺🇺🇸",
    "NZDUSD": "🇳🇿🇺🇸",
    "BTCUSD": "₿🇺🇸",
}

# تعداد رقم اعشار قیمت؛ نمادهای ذکرنشده: ین 3 رقم، بقیه 5 رقم
DECIMALS = {
    "BTCUSD": 0,
    "XAUUSD": 2,
}

# سشن‌ها: ساعت محلی هر بازار (تقریبی؛ تغییر ساعت تابستانی خودکار اعمال می‌شود)
SESSIONS = [
    ("Sydney", "Australia/Sydney", 7, 16),
    ("Tokyo", "Asia/Tokyo", 9, 18),
    ("London", "Europe/London", 8, 17),
    ("New York", "America/New_York", 8, 17),
]


def label(name):
    return f"{FLAGS.get(name, '')} {name}".strip()


def decimals(name):
    if name in DECIMALS:
        return DECIMALS[name]
    return 3 if "JPY" in name else 5
