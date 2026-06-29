"""
openvurp Core — Presenza

Dove si trova l'owner adesso? Un agente vero non urla nella stanza vuota:
guarda da quale canale l'owner ha parlato per ultimo e gli scrive lì.

Il runtime registra un "tocco" a ogni turno interattivo (CLI, Telegram,
Discord...) — mai per i cicli autonomi. Quando l'agente prende
l'iniziativa, chiede a questo modulo dove consegnare il messaggio:

- attivo di recente sulla TUI → messaggio nella TUI
- attivo di recente su Telegram (o assente da tutto) → Telegram,
  perché il telefono lo raggiunge anche lontano dalla scrivania
"""

from __future__ import annotations

import json
import os
import time

PRESENCE_FILE = os.path.join("runtime", "presence.json")

# Considerato "presente" su un canale se ha parlato lì negli ultimi N secondi
ACTIVE_WINDOW_SECONDS = 30 * 60

AUTONOMOUS_SOURCES = ("heartbeat", "cron", "subagent", "system")


class Presence:
    def __init__(self, memory_dir: str):
        self.path = os.path.join(memory_dir, PRESENCE_FILE)

    def _load(self) -> dict:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def touch(self, channel: str):
        """Registra che l'owner ha appena parlato su questo canale."""
        channel = (channel or "").strip().lower()
        if not channel or channel in AUTONOMOUS_SOURCES:
            return
        data = self._load()
        data[channel] = time.time()
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(data, f)
        except OSError:
            pass

    def last_seen(self, channel: str) -> float:
        """Timestamp dell'ultima attività sul canale (0 = mai visto)."""
        return float(self._load().get((channel or "").lower(), 0) or 0)

    def current_channel(self, window_seconds: int = ACTIVE_WINDOW_SECONDS) -> str:
        """Il canale dove l'owner è attivo ADESSO, o "" se assente ovunque."""
        now = time.time()
        best_channel, best_ts = "", 0.0
        for channel, ts in self._load().items():
            try:
                ts = float(ts)
            except (TypeError, ValueError):
                continue
            if now - ts <= window_seconds and ts > best_ts:
                best_channel, best_ts = channel, ts
        return best_channel

    def pick_delivery_channel(self, available: list[str],
                              window_seconds: int = ACTIVE_WINDOW_SECONDS) -> str:
        """Sceglie dove consegnare un messaggio proattivo.

        Regole:
        1. Se l'owner è attivo ora su un canale disponibile → lì
        2. Altrimenti, se c'è un canale "remoto" (telegram/discord/slack)
           → lì, perché raggiunge l'owner anche lontano dal computer
        3. Altrimenti il primo disponibile (tipicamente la TUI)
        """
        available = [c.lower() for c in available if c]
        if not available:
            return ""
        current = self.current_channel(window_seconds)
        if current in available:
            return current
        for remote in ("telegram", "discord", "slack", "signal"):
            if remote in available:
                return remote
        return available[0]
