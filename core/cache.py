"""
openvurp Core — LLM Response Cache

Cache disk-based con TTL per evitare chiamate duplicate.
"""

from __future__ import annotations

import os
import json
import hashlib
import time


class LLMCache:
    """Cache su disco per risposte LLM. Chiave = hash dei messaggi."""

    def __init__(self, cache_dir: str, ttl_seconds: int = 300, max_entries: int = 500):
        self.cache_dir = cache_dir
        self.ttl = ttl_seconds
        self.max_entries = max_entries
        os.makedirs(cache_dir, exist_ok=True)

    def _make_key(self, messages: list[dict], model: str = "") -> str:
        """Hash dei messaggi come chiave cache."""
        data = json.dumps(messages, sort_keys=True, ensure_ascii=False) + model
        return hashlib.sha256(data.encode()).hexdigest()[:24]

    def get(self, messages: list[dict], model: str = "") -> str | None:
        """Cerca in cache. None se miss o scaduta."""
        key = self._make_key(messages, model)
        path = os.path.join(self.cache_dir, f"{key}.json")

        if not os.path.exists(path):
            return None

        try:
            with open(path, "r", encoding="utf-8") as f:
                entry = json.load(f)

            # Check TTL
            if time.time() - entry.get("ts", 0) > self.ttl:
                os.remove(path)
                return None

            return entry.get("response", None)
        except Exception:
            return None

    def put(self, messages: list[dict], response: str, model: str = ""):
        """Salva risposta in cache."""
        key = self._make_key(messages, model)
        path = os.path.join(self.cache_dir, f"{key}.json")

        entry = {
            "ts": time.time(),
            "model": model,
            "response": response,
        }

        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(entry, f, ensure_ascii=False)
        except Exception:
            pass

    def cleanup(self):
        """Rimuovi entry scadute e tieni max_entries."""
        if not os.path.exists(self.cache_dir):
            return

        entries = []
        for f in os.listdir(self.cache_dir):
            if not f.endswith(".json"):
                continue
            path = os.path.join(self.cache_dir, f)
            try:
                mtime = os.path.getmtime(path)
                # Rimuovi scadute
                if time.time() - mtime > self.ttl:
                    os.remove(path)
                else:
                    entries.append((mtime, path))
            except Exception:
                pass

        # Se troppe entry, rimuovi le piu vecchie
        if len(entries) > self.max_entries:
            entries.sort()
            for _, path in entries[:len(entries) - self.max_entries]:
                try:
                    os.remove(path)
                except Exception:
                    pass

    @property
    def size(self) -> int:
        """Numero entry in cache."""
        if not os.path.exists(self.cache_dir):
            return 0
        return len([f for f in os.listdir(self.cache_dir) if f.endswith(".json")])
