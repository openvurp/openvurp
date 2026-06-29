"""
openvurp Core — Daily budget

Tetto giornaliero di chiamate LLM. Protegge dai loop impazziti (o indotti
da un prompt injection) che brucerebbero credito API. Persistente su disco,
si azzera ogni giorno. 0 = illimitato.
"""

from __future__ import annotations

import json
import os
from datetime import datetime


class DailyBudget:
    def __init__(self, memory_dir: str, max_calls: int = 0):
        self.max_calls = max(0, int(max_calls or 0))
        self.path = os.path.join(memory_dir, "runtime", "daily_budget.json")
        self._day = ""
        self._calls = 0
        self._load()

    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self._day = data.get("day", "")
            self._calls = int(data.get("calls", 0))
        except Exception:
            self._day, self._calls = "", 0
        self._roll()

    def _roll(self):
        today = datetime.now().date().isoformat()
        if self._day != today:
            self._day = today
            self._calls = 0

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump({"day": self._day, "calls": self._calls}, f)
        except Exception:
            pass

    def over_budget(self) -> bool:
        if self.max_calls <= 0:
            return False
        self._roll()
        return self._calls >= self.max_calls

    def record_call(self):
        self._roll()
        self._calls += 1
        self._save()

    def status(self) -> str:
        self._roll()
        if self.max_calls <= 0:
            return f"{self._calls} chiamate LLM oggi (nessun tetto)"
        return f"{self._calls}/{self.max_calls} chiamate LLM oggi"
