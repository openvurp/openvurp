"""
openvurp Tool — Scheduler

Permette all'agente di programmare messaggi, promemoria e azioni future.
Un thread in background controlla ogni 30s se ci sono notifiche da inviare.
La schedule persiste su disco — sopravvive ai riavvii.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timedelta

from core.tools import Tool, ToolResult, ErrorType

OPENVURP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCHEDULE_PATH = os.path.join(OPENVURP_DIR, "memory", "schedule.json")


# ── Schedule persistence ──

def _load_schedule() -> list[dict]:
    try:
        with open(SCHEDULE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save_schedule(entries: list[dict]):
    os.makedirs(os.path.dirname(SCHEDULE_PATH), exist_ok=True)
    with open(SCHEDULE_PATH, "w", encoding="utf-8") as f:
        json.dump(entries, f, indent=2, ensure_ascii=False)


# ── Scheduler thread ──

_scheduler_running = False
_scheduler_lock = threading.Lock()


def start_scheduler():
    """Avvia il thread scheduler in background. Chiamato da main.py."""
    global _scheduler_running
    with _scheduler_lock:
        if _scheduler_running:
            return
        _scheduler_running = True

    t = threading.Thread(target=_scheduler_loop, daemon=True, name="openvurp-scheduler")
    t.start()


def _scheduler_loop():
    """Loop principale: controlla ogni 30 secondi se ci sono notifiche da inviare."""
    while True:
        try:
            _check_and_send()
        except Exception:
            pass
        time.sleep(30)


def _check_and_send():
    """Controlla la schedule e invia le notifiche scadute."""
    entries = _load_schedule()
    if not entries:
        return

    now = datetime.now()
    remaining = []
    sent = False

    for entry in entries:
        scheduled_time = entry.get("when", "")
        try:
            when = datetime.fromisoformat(scheduled_time)
        except (ValueError, TypeError):
            continue

        if now >= when:
            # È il momento — invia
            _execute_entry(entry)
            sent = True
        else:
            remaining.append(entry)

    if sent:
        _save_schedule(remaining)


def _execute_entry(entry: dict):
    """Esegue una entry della schedule."""
    message = entry.get("message", "")
    channel = entry.get("channel", "telegram")
    voice = entry.get("voice", False)

    if not message:
        return

    if channel == "telegram":
        from tools.notify import _get_telegram, _send_telegram, _send_telegram_voice
        token, chat_id = _get_telegram()
        if not token:
            return

        # Prefisso per i promemoria
        label = entry.get("label", "")
        if label:
            full_message = f"⏰ {label}\n\n{message}"
        else:
            full_message = f"⏰ {message}"

        _send_telegram(token, chat_id, full_message)

        voice_enabled = False
        try:
            import config as cfg
            voice_enabled = bool(getattr(cfg, "VOICE_ENABLED", False))
        except Exception:
            voice_enabled = False

        if voice and voice_enabled:
            try:
                from voice import speak
                audio_path = speak(message, play=False)
                if audio_path:
                    _send_telegram_voice(token, chat_id, audio_path)
            except Exception:
                pass


# ── Tool handlers ──

def _parse_when(when_str: str) -> datetime | None:
    """Parsa il parametro 'when' in modo flessibile.

    Accetta:
      - ISO format: "2026-03-31T15:30:00"
      - Relativo: "30m", "2h", "1h30m", "90s", "1d"
      - Orario oggi: "15:30", "9:00"
    """
    when_str = when_str.strip()

    # 1. ISO format
    try:
        return datetime.fromisoformat(when_str)
    except ValueError:
        pass

    # 2. Relativo: 30m, 2h, 1h30m, 90s, 1d
    import re
    rel_pattern = re.compile(
        r'(?:(\d+)\s*d(?:ays?)?)?\s*'
        r'(?:(\d+)\s*h(?:ours?)?)?\s*'
        r'(?:(\d+)\s*m(?:in(?:ut[ei]?)?)?)?\s*'
        r'(?:(\d+)\s*s(?:ec(?:ond[ie]?)?)?)?',
        re.IGNORECASE,
    )
    m = rel_pattern.fullmatch(when_str)
    if m and any(m.groups()):
        days = int(m.group(1) or 0)
        hours = int(m.group(2) or 0)
        minutes = int(m.group(3) or 0)
        seconds = int(m.group(4) or 0)
        delta = timedelta(days=days, hours=hours, minutes=minutes, seconds=seconds)
        if delta.total_seconds() > 0:
            return datetime.now() + delta

    # 3. Orario oggi: "15:30" o "9:00"
    time_pattern = re.compile(r'^(\d{1,2}):(\d{2})$')
    tm = time_pattern.match(when_str)
    if tm:
        h, mi = int(tm.group(1)), int(tm.group(2))
        target = datetime.now().replace(hour=h, minute=mi, second=0, microsecond=0)
        if target <= datetime.now():
            target += timedelta(days=1)  # domani se l'ora è passata
        return target

    return None


def schedule_notify_handler(message: str, when: str, label: str = "",
                            voice: bool = False) -> ToolResult:
    """Programma un messaggio da inviare all'utente in futuro."""
    if not message.strip():
        return ToolResult.fail("No message to schedule")

    target = _parse_when(when)
    if not target:
        return ToolResult.fail(
            f"Non capisco quando: '{when}'. "
            f"Usa formato: '30m', '2h', '1h30m', '15:30', o ISO '2026-03-31T15:30:00'"
        )

    # Aggiungi alla schedule
    entries = _load_schedule()
    entry = {
        "message": message,
        "when": target.isoformat(),
        "label": label,
        "voice": voice,
        "channel": "telegram",
        "created": datetime.now().isoformat(),
    }
    entries.append(entry)
    _save_schedule(entries)

    # Assicurati che lo scheduler sia in esecuzione
    start_scheduler()

    time_str = target.strftime("%H:%M del %d/%m")
    delta = target - datetime.now()
    if delta.total_seconds() < 3600:
        tra = f"{int(delta.total_seconds() / 60)} minuti"
    elif delta.total_seconds() < 86400:
        h = int(delta.total_seconds() / 3600)
        m = int((delta.total_seconds() % 3600) / 60)
        tra = f"{h}h {m}m" if m else f"{h}h"
    else:
        tra = f"{int(delta.total_seconds() / 86400)} giorni"

    return ToolResult.ok(
        f"Programmato per le {time_str} (tra {tra})"
        + (f" — vocale" if voice else "")
    )


def list_schedule_handler() -> ToolResult:
    """Mostra i messaggi programmati."""
    entries = _load_schedule()
    if not entries:
        return ToolResult.ok("No scheduled messages.")

    lines = []
    for i, entry in enumerate(entries, 1):
        when = entry.get("when", "?")
        try:
            dt = datetime.fromisoformat(when)
            when_str = dt.strftime("%H:%M del %d/%m/%Y")
        except Exception:
            when_str = when
        msg = entry.get("message", "")[:80]
        voice = " [vocale]" if entry.get("voice") else ""
        lines.append(f"  {i}. {when_str} — {msg}{voice}")

    return ToolResult.ok("Messaggi programmati:\n" + "\n".join(lines))


def cancel_schedule_handler(index: int = 0) -> ToolResult:
    """Cancella un messaggio programmato (per indice, 1-based). Se 0, cancella tutti."""
    entries = _load_schedule()
    if not entries:
        return ToolResult.ok("No message to cancel.")

    if index == 0:
        _save_schedule([])
        return ToolResult.ok(f"Cancellati tutti ({len(entries)} messaggi)")

    if index < 1 or index > len(entries):
        return ToolResult.fail(f"Invalid index. You have {len(entries)} scheduled messages.")

    removed = entries.pop(index - 1)
    _save_schedule(entries)
    return ToolResult.ok(f"Cancellato: {removed.get('message', '')[:60]}")


# ── Tool definitions ──

SCHEDULE_NOTIFY_TOOL = Tool(
    name="schedule_notify",
    description=(
        "Programma un messaggio da inviare in futuro su Telegram. "
        "Puoi programmare promemoria, avvisi, messaggi a qualsiasi orario. "
        "Formato 'when': '30m' (30 minuti), '2h' (2 ore), '15:30' (orario), "
        "'1d' (1 giorno), o ISO datetime."
    ),
    parameters={
        "type": "object",
        "properties": {
            "message": {
                "type": "string",
                "description": "Il messaggio da inviare",
            },
            "when": {
                "type": "string",
                "description": "Quando inviare: '30m', '2h', '15:30', '1d', ISO datetime",
            },
            "label": {
                "type": "string",
                "description": "Etichetta breve per il promemoria (opzionale)",
            },
            "voice": {
                "type": "boolean",
                "description": "Invia anche come messaggio vocale (default: false)",
            },
        },
        "required": ["message", "when"],
    },
    handler=schedule_notify_handler,
    timeout=10,
)

LIST_SCHEDULE_TOOL = Tool(
    name="list_schedule",
    description="Mostra tutti i messaggi e promemoria programmati.",
    parameters={
        "type": "object",
        "properties": {},
        "required": [],
    },
    handler=list_schedule_handler,
    timeout=5,
)

CANCEL_SCHEDULE_TOOL = Tool(
    name="cancel_schedule",
    description="Cancella un messaggio programmato. Indice 0 = cancella tutti.",
    parameters={
        "type": "object",
        "properties": {
            "index": {
                "type": "number",
                "description": "Numero del messaggio da cancellare (1, 2, 3...). 0 = tutti.",
            },
        },
        "required": [],
    },
    handler=cancel_schedule_handler,
    timeout=5,
)
