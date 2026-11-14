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
| `bot/storage.py` | Users and last FIRE per symbol: Postgres when `DATABASE_URL` is set, else `bot_state.json` |
| `bot/telegram_bot.py` | Telegram commands and broadcast |
| `bot/scanner.py` | Live scan loop |
| `bot/main.py` | Entry point (web server + scanner + Telegram) |

## Run

```
pip install -r requirements.txt
BOT_TOKEN=... python -m bot.main
```

Environment variables:

- `BOT_TOKEN`: Telegram bot token (without it, alerts are printed to the console).
- `DATABASE_URL`: Postgres connection string, e.g. from Neon. Without it, state lives in `bot_state.json`, which Render wipes on every deploy. On the first start with a database, users from an existing `bot_state.json` are copied in.

## Tests

```
python -m unittest discover -s tests -v
```

The Postgres storage tests run only when `TEST_DATABASE_URL` points at a throwaway database (its tables are dropped).

`tests/test_strategy.py` replays deterministic synthetic candles and checks that every alert matches `tests/golden_events.json` exactly. If you change the signal logic or message text on purpose, regenerate it with `python tests/test_strategy.py --regen` and review the diff.
