"""تشخیص دو نوع فلگ (طرح بردیا) و اثرش روی واگرایی (48,104,36):

  میله  : حرکت قیمت قبل از قله/کف اول واگرایی (POLE_BARS کندل)
  فلگ   : قیمت بین قله/کف اول و قله/کف دوم واگرایی

  فلگ برگشت (زاویه‌ی باز، «وج»): فلگ در جهت میله ادامه می‌دهد (شیب نسبی مثبت)
                                   و نوسان‌ها کوچک‌تر می‌شوند (همگرا) ← احتمال برگشت
  فلگ ادامه (زاویه‌ی تند)       : فلگ افقی یا خلاف میله

  برای واگرایی صعودی میله رو به پایین است (فشار فروش)؛ وج = فلگ رو به پایینِ همگرا.

classify(df_arrays, i_first, i_second, side) → dict ویژگی‌ها
"""
import numpy as np

POLE_BARS = 60


def classify(h, l, c, atr, i1, i2, side):
    """side = جهت معامله (+1 خرید پس از واگرایی صعودی). جهت میله = -side."""
    pole = -side
    n = i2 - i1 + 1
    if n < 6 or i1 - POLE_BARS < 0:
        return None
    a = atr[i2] if atr[i2] > 0 else np.nan
    # شیب میله و فلگ (رگرسیون خطی Close، به واحد ATR در هر کندل)
    xs = np.arange(POLE_BARS + 1)
    pole_slope = np.polyfit(xs, c[i1 - POLE_BARS:i1 + 1], 1)[0] / a
    xf = np.arange(n)
    flag_slope = np.polyfit(xf, c[i1:i2 + 1], 1)[0] / a
    rel = flag_slope * pole                       # >0: فلگ در جهت میله ادامه داده
    # همگرایی: دامنه‌ی نیمه‌ی دوم فلگ کمتر از نیمه‌ی اول؟
    m = i1 + n // 2
    amp1 = h[i1:m].max() - l[i1:m].min()
    amp2 = h[m:i2 + 1].max() - l[m:i2 + 1].min()
    converging = amp2 < amp1
    # شیب خط سقف‌ها و کف‌ها
    top_slope = np.polyfit(xf, h[i1:i2 + 1], 1)[0] / a
    bot_slope = np.polyfit(xf, l[i1:i2 + 1], 1)[0] / a
    wedge = rel > 0 and converging
    return {"pole_slope": pole_slope * pole, "flag_rel_slope": rel, "converging": converging,
            "top_slope": top_slope * pole, "bot_slope": bot_slope * pole,
            "flag_type": "reversal (wedge)" if wedge else "continuation"}
