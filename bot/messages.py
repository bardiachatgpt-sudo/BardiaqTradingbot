"""ساخت متن پیام‌های تلگرام (انگلیسی، HTML).

جزئیات واگرایی پشت صحنه می‌ماند؛ پیام فقط جهت، قیمت لحظه و زمان را نشان می‌دهد.
"""
import pytz

from .config import decimals, label
from .timefmt import session_at, to_tehran

LONDON = pytz.timezone("Europe/London")
LINE = "━" * 16


def fmt_price(sym, x):
    return f"{x:.{decimals(sym)}f}"


def to_london(ts):
    return ts.tz_convert(LONDON).strftime("%Y-%m-%d %H:%M")


def build_msg(ev):
    s, sym, t = ev["s"], ev["sym"], ev["time"]
    side = "BULLISH📈" if s > 0 else "BEARISH📉"
    icon = "🟢" if s > 0 else "🔴"

    if ev["kind"] == "READY":
        head = f"{icon} <b>READY · {side}</b>"
    elif ev["kind"] == "FIRE":
        head = f"🔥 <b>FIRE · {side}</b>" + ("  ⭐ <b>STRONG</b>" if ev["strong"] else "")
    else:
        head = f"✖️ <b>CANCELLED · {side}</b>"

    txt = (f"{head}\n"
           f"<b>{label(sym)}</b>  ·  M1\n{LINE}\n"
           f"💰 Price: <b>{fmt_price(sym, ev['price'])}</b>\n"
           f"🕒 <b>{to_tehran(t)}</b> Tehran\n"
           f"🕒 <b>{to_london(t)}</b> London (for Kamyar)\n"
           f"🌍 {session_at(t)}")

    if ev["kind"] == "INVALID":
        txt += f"\n{LINE}\n<i>Setup cancelled · no trade</i>"
    return txt
