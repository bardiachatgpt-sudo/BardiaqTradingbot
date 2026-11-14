# MACD Divergence Scanner Bot

Scans M1 candles from TradingView's WebSocket for MACD (48,104,36) divergences, follows each one through READY → FIRE / CANCELLED, and sends the alerts to Telegram.

## Layout

| File | What it does |
|---|---|
| `bot/config.py` | Settings, symbols and data sources, flags, price decimals, sessions |
| `bot/data.py` | Fetches candles from TradingView |
| `bot/indicators.py` | EMA and MACD |
| `bot/strategy.py` | Divergence detection, READY/FIRE cycle, per-symbol engine |
| `bot/messages.py` | Telegram message text |
| `bot/timefmt.py` | Tehran time, sessions, weekend |
| `bot/storage.py` | Users and last FIRE per symbol (`bot_state.json`) |
| `bot/telegram_bot.py` | Telegram commands and broadcast |
| `bot/scanner.py` | Live scan loop |
| `bot/main.py` | Entry point (web server + scanner + Telegram) |

## Run

```
pip install -r requirements.txt
BOT_TOKEN=... python -m bot.main
```

## Tests

```
python -m unittest discover -s tests -v
```

`tests/test_strategy.py` replays deterministic synthetic candles and checks that every alert matches `tests/golden_events.json` exactly. If you change the signal logic or message text on purpose, regenerate it with `python tests/test_strategy.py --regen` and review the diff.
