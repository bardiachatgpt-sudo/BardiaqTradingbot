"""ساخت متن پیام‌های تلگرام (انگلیسی، HTML)."""
from .config import decimals, label
from .timefmt import fmt_gap, session_at, to_tehran, to_tehran_short


def fmt_price(sym, x):
    return f"{x:.{decimals(sym)}f}"


def fmt_h(x):
    return f"{x:.4g}"


LINE = "━" * 16
LINE2 = "━" * 26


def detail_block(ev):
    s, sym, d = ev["s"], ev["sym"], ev["detail"]
    low, opp = ("low", "high") if s > 0 else ("high", "low")
    mark = "🔻" if s > 0 else "🔺"

    def pt(k):
        h, p, t = d[k]
        return fmt_price(sym, p), to_tehran_short(t), fmt_h(h)

    p1, t1, h1 = pt("first")
    pm, tm, hm = pt("mid")
    p2, t2, h2 = pt("second")
    return (f"\n{LINE2}\n"
            "📐 <b>Divergence · MACD (48,104,36)</b>\n"
            f"Confirmed: {to_tehran_short(ev['div'])}\n"
            f"{mark} <b>1st {low}</b>  ·  {p1}  ·  {t1}\n"
            f"{mark} <b>2nd {low}</b>  ·  {p2}  ·  {t2}\n"
            f"▫️ Mid {opp}  ·  {pm}  ·  {tm}\n"
            f"📊 Hist: 1st {h1} | mid {hm} | 2nd {h2}")


def build_msg(ev):
    s = ev["s"]
    side = "BULLISH📈" if s > 0 else "BEARISH📉"
    icon = "🟢" if s > 0 else "🔴"
    pos_w = "above" if s > 0 else "below"
    neg_w = "below" if s > 0 else "above"
    low = "low" if s > 0 else "high"
    t = ev["time"]
    head2 = f"<b>{label(ev['sym'])}</b>  ·  M1\n{LINE}\n"
    when = f"🕒 <b>{to_tehran(t)}</b> Tehran\n🌍 {session_at(t)}\n"

    if ev["kind"] == "READY":
        return (f"{icon} <b>READY · {side}</b>\n" + head2 + when +
                f"✅ MACD (12,26,9) {pos_w} zero for {ev['pos2']} bars\n"
                f"👀 Now watching MACD (3,6,2) for the FIRE"
                + detail_block(ev))

    if ev["kind"] == "INVALID":
        if ev["reason"] == "price":
            why = (f"Price broke {neg_w} the 2nd {low} "
                   f"({fmt_price(ev['sym'], ev['detail']['second'][1])})")
        elif ev["reason"] == "deeper":
            why = (f"MACD (48,104,36) printed a deeper {low} than the 1st one — "
                   f"divergence reference is void")
        else:
            why = f"MACD (12,26,9) crossed {neg_w} zero"
        return (f"⚪ <b>CANCELLED · {side}</b>\n" + head2 + when +
                f"❌ {why}\n"
                f"READY was at {to_tehran_short(ev['ready'])}"
                + detail_block(ev))

    # FIRE
    if ev["path"] == 1:
        path = f"first {'trough' if s > 0 else 'peak'} confirmed"
    else:
        path = f"back {'above' if s > 0 else 'below'} zero"
    strong = "  ⭐ <b>STRONG</b>" if ev["strong"] else ""
    txt = (f"🔥 <b>FIRE · {side}</b>{strong}\n" + head2 + when +
           f"⚡ Trigger: path {ev['path']} — {path}\n"
           f"⏱ Divergence → FIRE: {fmt_gap(t - ev['div'])}")
    if ev["strong"] and ev["m5_time"] is not None:
        txt += f"\n📈 M5 divergence confirmed: {to_tehran_short(ev['m5_time'])}"
    return txt + detail_block(ev)
