"""ذخیره‌ی کاربران و آخرین FIRE هر نماد در فایل JSON."""
import json
import os
import threading

import pandas as pd


class State:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.users = set()
        self.last_fire = {}   # {symbol: {"t": iso_utc, "d": +1/-1, "msg": متن کامل پیام FIRE}}

    def load(self):
        try:
            if os.path.exists(self.path):
                with open(self.path) as f:
                    d = json.load(f)
                self.users = set(d.get("users", []))
                self.last_fire = d.get("last_fire", {})
        except Exception as e:
            print("load_state error:", e)
        return self

    def save(self):
        try:
            with self.lock:
                with open(self.path, "w") as f:
                    json.dump({"users": list(self.users), "last_fire": self.last_fire}, f)
        except Exception as e:
            print("save_state error:", e)

    def add_user(self, chat_id):
        self.users.add(chat_id)
        self.save()

    def record_fire(self, name, s, ts, msg=None):
        cur = self.last_fire.get(name)
        if cur is None or ts > pd.Timestamp(cur["t"]):
            self.last_fire[name] = {"t": ts.isoformat(), "d": s, "msg": msg}
            self.save()
