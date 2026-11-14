"""ربات تلگرام: ثبت کاربر، دکمه‌ی آخرین FIRE و ارسال پیام همگانی."""
import time

import pandas as pd
import telebot
from telebot import types

from .config import SYMBOLS, label
from .timefmt import to_tehran


class Telegram:
    def __init__(self, token, state):
        self.state = state
        self.bot = telebot.TeleBot(token) if token else None
        if self.bot:
            self._register_handlers()

    def broadcast(self, text):
        if not self.bot:
            print(text)
            return
        for chat_id in list(self.state.users):
            try:
                self.bot.send_message(chat_id, text, parse_mode="HTML")
            except Exception as e:
                print("telegram send error:", e)

    def keyboard(self):
        kb = types.InlineKeyboardMarkup(row_width=2)
        kb.add(*[types.InlineKeyboardButton(label(n), callback_data="last:" + n) for n in SYMBOLS])
        return kb

    def last_fire_text(self, name):
        r = self.state.last_fire.get(name)
        if not r:
            return f"{label(name)}: no FIRE recorded yet"
        if r.get("msg"):
            return r["msg"]
        side = "bullish" if r["d"] > 0 else "bearish"
        return f"{label(name)}: {to_tehran(pd.Timestamp(r['t']))} Tehran ({side})"

    def _register_handlers(self):
        bot = self.bot

        @bot.message_handler(commands=["start", "last"])
        def on_start(m):
            self.state.add_user(m.chat.id)
            bot.send_message(m.chat.id,
                             "✅ Registered. You will receive READY and FIRE alerts.\n"
                             "Tap a pair to see its last FIRE:",
                             reply_markup=self.keyboard())

        @bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("last:"))
        def on_last(c):
            name = c.data.split(":", 1)[1]
            bot.answer_callback_query(c.id)
            if name in SYMBOLS:
                try:
                    bot.send_message(c.message.chat.id, self.last_fire_text(name), parse_mode="HTML")
                except Exception:
                    bot.send_message(c.message.chat.id, self.last_fire_text(name))

    def run(self):
        """حلقه‌ی اصلی (مسدودکننده)."""
        if self.bot:
            self.bot.infinity_polling(skip_pending=True)
        else:
            while True:
                time.sleep(60)
