"""ساخت متن پیام‌های تلگرام (انگلیسی، HTML).

جزئیات واگرایی پشت صحنه می‌ماند؛ پیام فقط جهت، قیمت لحظه و زمان را نشان می‌دهد.
"""
import pandas as pd
import pytz

from .config import TEHRAN, decimals, label
from .timefmt import session_at

LONDON = pytz.timezone("Europe/London")
LINE = "━" * 16


def fmt_price(sym, x):
    return f"{x:.{decimals(sym)}f}"


def fmt_time(ts, tz):
    """مثلاً: Saturday 2026-11-14 04:37 (روز هفته به وقت همان شهر)"""
    return ts.tz_convert(tz).strftime("%A %Y-%m-%d %H:%M")


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
           f"🕒 <b>{fmt_time(t, TEHRAN)}</b> Tehran\n"
           f"🕒 <b>{fmt_time(t, LONDON)}</b> London (for Kamyar)\n"
           f"🌍 {session_at(t)}")

    if ev["kind"] == "INVALID":
        txt += f"\n{LINE}\n<i>Setup cancelled · no trade</i>"
    return txt


def fire_from_record(name, r):
    """پیام FIRE از داده‌ی ذخیره‌شده (دکمه‌ی آخرین FIRE) با قالب فعلی."""
    return build_msg({"kind": "FIRE", "sym": name, "s": r["d"], "time": pd.Timestamp(r["t"]),
                      "price": r["price"], "strong": bool(r.get("strong"))})
