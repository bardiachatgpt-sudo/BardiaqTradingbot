"""اندیکاتورها."""


class EMA:
    def __init__(self, span):
        self.a = 2.0 / (span + 1)
        self.v = None

    def update(self, x):
        self.v = x if self.v is None else self.a * x + (1 - self.a) * self.v
        return self.v


class MACDHist:
    """میله‌ها = خط MACD (مثل MT5)، نه MACD منهای Signal."""

    def __init__(self, fast, slow, sig):
        self.f, self.s = EMA(fast), EMA(slow)

    def update(self, close):
        return self.f.update(close) - self.s.update(close)
