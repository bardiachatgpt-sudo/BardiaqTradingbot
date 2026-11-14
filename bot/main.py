"""نقطه‌ی شروع: وب‌سرور Render + اسکنر + ربات تلگرام.

اجرا:  python -m bot.main
"""
import os
import threading

from flask import Flask

from .config import STATE_FILE
from .scanner import run_scanner
from .storage import State
from .telegram_bot import Telegram

app = Flask(__name__)


@app.route("/")
def home():
    return "MACD divergence scanner is live"


def run_web():
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 7860)))


def main():
    state = State(STATE_FILE).load()
    tg = Telegram(os.environ.get("BOT_TOKEN"), state)
    threading.Thread(target=run_web, daemon=True).start()
    threading.Thread(target=run_scanner, args=(state, tg.broadcast), daemon=True).start()
    tg.run()


if __name__ == "__main__":
    main()
