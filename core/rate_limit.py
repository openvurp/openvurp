"""
openvurp Core — Rate Limiting

Limita messaggi per sender/canale per evitare spam e costi eccessivi.
"""

from __future__ import annotations

import time
import threading


class RateLimiter:
    """Rate limiter per sender con cooldown e burst limit."""

    def __init__(self, cooldown_seconds: float = 2.0, max_burst: int = 5,
                 burst_window: int = 60):
        self.cooldown = cooldown_seconds
        self.max_burst = max_burst
        self.burst_window = burst_window
        self._last_message: dict[str, float] = {}
        self._burst_count: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def check(self, sender: str) -> tuple[bool, str]:
        """Controlla se il sender puo inviare.

        Returns: (allowed, reason)
        """
        now = time.time()

        with self._lock:
            # Cooldown check
            last = self._last_message.get(sender, 0)
            if now - last < self.cooldown:
                wait = round(self.cooldown - (now - last), 1)
                return False, f"Aspetta {wait}s"

            # Burst check
            if sender not in self._burst_count:
                self._burst_count[sender] = []

            # Rimuovi timestamp vecchi
            self._burst_count[sender] = [
                t for t in self._burst_count[sender]
                if now - t < self.burst_window
            ]

            if len(self._burst_count[sender]) >= self.max_burst:
                return False, f"Troppi messaggi ({self.max_burst} in {self.burst_window}s)"

            # Permetti
            self._last_message[sender] = now
            self._burst_count[sender].append(now)
            return True, ""

    def reset(self, sender: str = ""):
        """Reset limiti per un sender o tutti."""
        with self._lock:
            if sender:
                self._last_message.pop(sender, None)
                self._burst_count.pop(sender, None)
            else:
                self._last_message.clear()
                self._burst_count.clear()
