"""
openvurp Core — Runtime Gateway

Punto di separazione leggero tra runtime, canali e UI.
Tiene anche un event log persistente per debugging e UI future.
"""

from __future__ import annotations

import json
import os
import time
from typing import Callable


class RuntimeGateway:
    def __init__(self, workspace_dir: str = ""):
        self._announcers: dict[str, Callable] = {}
        self._event_listeners: list[Callable[[str, dict], None]] = []
        root = workspace_dir or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self._event_log = os.path.join(root, "memory", "runtime", "gateway_events.jsonl")
        os.makedirs(os.path.dirname(self._event_log), exist_ok=True)

    def register_announcer(self, channel: str, callback: Callable) -> None:
        if channel and callable(callback):
            self._announcers[str(channel)] = callback

    def unregister_announcer(self, channel: str) -> None:
        self._announcers.pop(str(channel), None)

    def register_event_listener(self, callback: Callable[[str, dict], None]) -> None:
        if callable(callback):
            self._event_listeners.append(callback)

    def _append_event(self, event_name: str, payload: dict | None = None) -> None:
        event = {
            "ts": time.time(),
            "event": event_name,
            "payload": dict(payload or {}),
        }
        try:
            with open(self._event_log, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def emit(self, event_name: str, payload: dict | None = None) -> None:
        data = dict(payload or {})
        self._append_event(event_name, data)
        for listener in list(self._event_listeners):
            try:
                listener(event_name, data)
            except Exception:
                pass

    def announce(self, route, text: str) -> bool:
        if not text:
            return False
        self._append_event(
            "gateway.announce",
            {
                "source": getattr(route, "source", ""),
                "chat_id": getattr(route, "chat_id", ""),
                "thread_id": getattr(route, "thread_id", ""),
                "session_key": getattr(route, "session_key", ""),
                "text_preview": str(text)[:500],
            },
        )
        callback = self._announcers.get(getattr(route, "source", "")) or self._announcers.get("default")
        if not callable(callback):
            return False
        try:
            callback(route, text)
            return True
        except Exception:
            return False
