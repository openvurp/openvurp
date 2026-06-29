"""
openvurp Core — Activity bus

Un piccolo pub/sub in-process: l'agente pubblica QUI ogni attività (messaggio
utente, token di risposta, passaggi/tool) da qualsiasi canale (CLI, Telegram,
heartbeat, dashboard). La dashboard si iscrive via SSE e mostra tutto in tempo
reale — così riflette ciò che succede nel terminale e non riparte mai da zero.

Single chokepoint: l'agente chiama publish() nei suoi punti di output, quindi
un solo posto cattura ogni sorgente.
"""

from __future__ import annotations

import queue
import threading
import time
from collections import deque

_subscribers: list[queue.Queue] = []
_lock = threading.Lock()
_recent: deque = deque(maxlen=300)  # storia recente per chi si connette dopo
_seq = 0


def publish(kind: str, **data) -> None:
    """Pubblica un evento a tutti gli iscritti (e nella storia recente)."""
    global _seq
    with _lock:
        _seq += 1
        evt = {"seq": _seq, "kind": kind, "ts": time.time()}
        evt.update(data)
        _recent.append(evt)
        subs = list(_subscribers)
    for q in subs:
        try:
            q.put_nowait(evt)
        except Exception:
            pass  # subscriber lento/pieno: non bloccare mai l'agente


def subscribe() -> tuple[queue.Queue, list]:
    """Ritorna (coda, storia_recente). La storia permette di non ripartire da zero."""
    q: queue.Queue = queue.Queue(maxsize=4000)
    with _lock:
        snapshot = list(_recent)
        _subscribers.append(q)
    return q, snapshot


def unsubscribe(q: queue.Queue) -> None:
    with _lock:
        try:
            _subscribers.remove(q)
        except ValueError:
            pass
