"""
openvurp Tool — Notify

Permette all'agente di inviare messaggi all'utente su Telegram (o altri canali)
in qualsiasi momento, senza bloccarsi. L'agente può contattare l'utente
anche quando non è in conversazione diretta.
"""

from __future__ import annotations

import json
import os
import threading

from core.tools import Tool, ToolResult, ErrorType


OPENVURP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TELEGRAM_STATE_PATH = os.path.join(OPENVURP_DIR, "memory", "telegram_state.json")


def _config_bool(name: str, default: bool = False) -> bool:
    try:
        import config as cfg
        value = getattr(cfg, name, default)
    except Exception:
        value = default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on", "si", "sì"}


def _get_telegram():
    """Ottieni token e chat_id da config."""
    try:
        import config as cfg
        token = getattr(cfg, "TELEGRAM_TOKEN", "")
        allowed = getattr(cfg, "TELEGRAM_ALLOWED_USERS", [])
        if not token:
            return None, None
        # L'ultimo chat_id lo scriveva il bot in ENTRATA, che non c'e' piu':
        # senza un destinatario esplicito la notifica non partirebbe piu'.
        chat_id = str(getattr(cfg, "TELEGRAM_CHAT_ID", "") or "").strip()
        if not chat_id:
            chat_id = _get_last_chat_id()
        if not chat_id and allowed:
            chat_id = str(allowed[0])
        if not chat_id:
            return None, None
        return token, chat_id
    except Exception:
        return None, None


def _get_last_chat_id() -> str:
    try:
        with open(TELEGRAM_STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return str(data.get("last_chat_id", "")).strip()
    except Exception:
        return ""


def _send_telegram(token: str, chat_id: str, text: str) -> bool:
    """Invia messaggio Telegram via API diretta (nessuna dipendenza extra)."""
    import requests
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        r = requests.post(url, json={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "Markdown",
        }, timeout=10)
        return r.ok
    except Exception:
        # Riprova senza Markdown (a volte fallisce per caratteri speciali)
        try:
            r = requests.post(url, json={
                "chat_id": chat_id,
                "text": text,
            }, timeout=10)
            return r.ok
        except Exception:
            return False


def _guess_telegram_method(path: str, force_document: bool = False) -> tuple[str, str]:
    if force_document:
        return "sendDocument", "document"

    ext = os.path.splitext(path)[1].lower()
    if ext in (".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"):
        return "sendPhoto", "photo"
    if ext in (".mp3", ".ogg", ".wav", ".flac", ".m4a", ".aac"):
        return "sendAudio", "audio"
    if ext in (".mp4", ".mov", ".avi", ".mkv", ".webm"):
        return "sendVideo", "video"
    if ext == ".opus":
        return "sendVoice", "voice"
    return "sendDocument", "document"


def _send_telegram_media(token: str, chat_id: str, path: str,
                         caption: str = "", force_document: bool = False) -> bool:
    import requests

    method, field = _guess_telegram_method(path, force_document=force_document)
    url = f"https://api.telegram.org/bot{token}/{method}"

    try:
        with open(path, "rb") as f:
            data = {"chat_id": chat_id}
            if caption:
                data["caption"] = caption[:1024]
            files = {field: (os.path.basename(path), f)}
            r = requests.post(url, data=data, files=files, timeout=60)
        return r.ok
    except Exception:
        return False


def _send_telegram_voice(token: str, chat_id: str, audio_path: str) -> bool:
    """Invia messaggio vocale su Telegram."""
    import requests
    url = f"https://api.telegram.org/bot{token}/sendVoice"
    try:
        with open(audio_path, "rb") as f:
            r = requests.post(url,
                data={"chat_id": chat_id},
                files={"voice": f},
                timeout=30)
        return r.ok
    except Exception:
        return False


def _send_telegram_poll(token: str, chat_id: str, question: str,
                        options: list, is_anonymous: bool = True,
                        allows_multiple_answers: bool = False) -> dict:
    """Invia un sondaggio nativo Telegram (sendPoll).

    Ritorna il JSON della risposta Telegram (dict). In caso di errore
    di rete ritorna {"ok": False, "error": "..."}.
    """
    import requests as _requests
    url = f"https://api.telegram.org/bot{token}/sendPoll"
    payload = {
        "chat_id": chat_id,
        "question": question,
        "options": options,
        "is_anonymous": bool(is_anonymous),
        "allows_multiple_answers": bool(allows_multiple_answers),
    }
    try:
        r = _requests.post(url, json=payload, timeout=15)
        try:
            return r.json()
        except Exception:
            return {"ok": False, "error": f"HTTP {r.status_code}: {r.text[:200]}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def notify_handler(message: str, urgent: bool = False,
                   voice: bool = False) -> ToolResult:
    """Invia un messaggio all'utente su Telegram.

    Non blocca l'agente — il messaggio viene inviato in background.
    """
    if not message.strip():
        return ToolResult.fail("No message to send")

    token, chat_id = _get_telegram()
    if not token:
        return ToolResult.fail(
            "Telegram non configurato. Serve TELEGRAM_TOKEN e TELEGRAM_ALLOWED_USERS in .env o nell'ambiente",
            error_type=ErrorType.DEPENDENCY,
        )

    # Invio asincrono — non blocca l'agente
    def _send():
        if voice and _config_bool("VOICE_ENABLED", False):
            try:
                from voice import speak
                audio_path = speak(message, play=False)
                if audio_path:
                    _send_telegram_voice(token, chat_id, audio_path)
            except Exception:
                pass
        _send_telegram(token, chat_id, message)

    t = threading.Thread(target=_send, daemon=True, name="notify-telegram")
    t.start()

    prefix = "URGENTE: " if urgent else ""
    return ToolResult.ok(f"{prefix}Message sent on Telegram")


def notify_voice_handler(message: str) -> ToolResult:
    """Invia un messaggio vocale all'utente su Telegram.

    Genera audio con TTS e lo invia come voice message.
    """
    if not message.strip():
        return ToolResult.fail("No message to send")
    if not _config_bool("VOICE_ENABLED", False):
        return ToolResult.fail(
            "Voce disattivata da configurazione (VOICE_ENABLED=0).",
            error_type=ErrorType.PERMISSION,
        )

    token, chat_id = _get_telegram()
    if not token:
        return ToolResult.fail(
            "Telegram non configurato",
            error_type=ErrorType.DEPENDENCY,
        )

    try:
        from voice import speak
    except ImportError:
        return ToolResult.fail(
            "edge-tts non installato (pip install edge-tts)",
            error_type=ErrorType.DEPENDENCY,
        )

    def _send():
        try:
            audio_path = speak(message, play=False)
            if audio_path:
                _send_telegram_voice(token, chat_id, audio_path)
                # Invia anche il testo come didascalia
                _send_telegram(token, chat_id, message)
        except Exception:
            # Fallback: solo testo
            _send_telegram(token, chat_id, message)

    t = threading.Thread(target=_send, daemon=True, name="notify-voice")
    t.start()

    return ToolResult.ok("Voice message sent on Telegram")


def notify_file_handler(path: str, caption: str = "", force_document: bool = False) -> ToolResult:
    if not path.strip():
        return ToolResult.fail("Percorso file vuoto", error_type=ErrorType.VALIDATION)

    full_path = path if os.path.isabs(path) else os.path.join(OPENVURP_DIR, path)
    full_path = os.path.normpath(full_path)
    if not os.path.isfile(full_path):
        return ToolResult.fail(f"File non trovato: {full_path}", error_type=ErrorType.NOT_FOUND)

    token, chat_id = _get_telegram()
    if not token:
        return ToolResult.fail(
            "Telegram non configurato",
            error_type=ErrorType.DEPENDENCY,
        )

    ok = _send_telegram_media(token, chat_id, full_path, caption=caption, force_document=force_document)
    if not ok:
        return ToolResult.fail("Invio file Telegram fallito", error_type=ErrorType.RUNTIME)

    return ToolResult.ok(f"File sent on Telegram: {os.path.basename(full_path)}")


def notify_photo_handler(path: str, caption: str = "") -> ToolResult:
    return notify_file_handler(path=path, caption=caption, force_document=False)


def notify_poll_handler(chat_id: str = "", question: str = "",
                        options: list | None = None,
                        is_anonymous: bool = True,
                        allows_multiple_answers: bool = False) -> ToolResult:
    """Invia un sondaggio nativo Telegram (sendPoll).

    - `chat_id`: se vuoto usa l'ultimo chat noto o il primo utente consentito.
    - `options`: lista di 2-10 stringhe (1-100 caratteri ciascuna).
    - `is_anonymous`: True di default.
    - `allows_multiple_answers`: False di default.
    """
    if not question.strip():
        return ToolResult.fail("Domanda del sondaggio vuota", error_type=ErrorType.VALIDATION)
    if not options or len(options) < 2:
        return ToolResult.fail(
            "Servono almeno 2 opzioni (Telegram ne accetta fino a 10)",
            error_type=ErrorType.VALIDATION,
        )
    if len(options) > 10:
        return ToolResult.fail(
            f"Troppe opzioni ({len(options)}): Telegram ne accetta al massimo 10",
            error_type=ErrorType.VALIDATION,
        )
    for o in options:
        if len(str(o)) > 100:
            return ToolResult.fail(
                f"Opzione troppo lunga ({len(str(o))} caratteri, max 100): {o!r}",
                error_type=ErrorType.VALIDATION,
            )

    token, default_chat = _get_telegram()
    if not token:
        return ToolResult.fail(
            "Telegram non configurato. Serve TELEGRAM_TOKEN e TELEGRAM_ALLOWED_USERS in .env o nell'ambiente",
            error_type=ErrorType.DEPENDENCY,
        )

    target_chat = (chat_id or default_chat or "").strip()
    if not target_chat:
        return ToolResult.fail(
            "Nessun chat_id: specifica chat_id oppure configura TELEGRAM_ALLOWED_USERS",
            error_type=ErrorType.VALIDATION,
        )

    # Le opzioni vanno serializzate come JSON-stringa (Telegram API)
    options_json = json.dumps([str(o) for o in options], ensure_ascii=False)
    resp = _send_telegram_poll(
        token, target_chat, question, options_json,
        is_anonymous=is_anonymous,
        allows_multiple_answers=allows_multiple_answers,
    )

    if not resp.get("ok"):
        return ToolResult.fail(
            f"Sondaggio non inviato: {resp.get('description') or resp.get('error') or 'errore sconosciuto'}",
            error_type=ErrorType.RUNTIME,
        )

    result = resp.get("result") or {}
    message_id = result.get("message_id")
    chat_obj = result.get("chat") or {}
    chat_title = chat_obj.get("title") or chat_obj.get("username") or str(target_chat)
    return ToolResult.ok(
        json.dumps({
            "ok": True,
            "message_id": message_id,
            "chat_id": target_chat,
            "chat_title": chat_title,
            "question": question,
            "options_count": len(options),
        }, ensure_ascii=False)
    )


# ── Tool definitions ──

NOTIFY_TOOL = Tool(
    name="notify",
    description=(
        "Invia un messaggio all'utente su Telegram. "
        "Usa per avvisare, aggiornare, o contattare l'utente in qualsiasi momento. "
        "Il messaggio viene inviato in background — non ti blocca."
    ),
    parameters={
        "type": "object",
        "properties": {
            "message": {
                "type": "string",
                "description": "Il messaggio da inviare all'utente",
            },
            "urgent": {
                "type": "boolean",
                "description": "Se True, il messaggio è marcato come urgente (default: False)",
            },
        },
        "required": ["message"],
    },
    handler=notify_handler,
    timeout=15,
)

NOTIFY_VOICE_TOOL = Tool(
    name="notify_voice",
    description=(
        "Invia un messaggio VOCALE all'utente su Telegram. "
        "Genera audio con la tua voce e lo manda come voice message. "
        "Usa quando vuoi che l'utente senta la tua voce."
    ),
    parameters={
        "type": "object",
        "properties": {
            "message": {
                "type": "string",
                "description": "Il testo da sintetizzare e inviare come audio",
            },
        },
        "required": ["message"],
    },
    handler=notify_voice_handler,
    timeout=30,
)

NOTIFY_FILE_TOOL = Tool(
    name="notify_file",
    description=(
        "Invia un file su Telegram. Rileva automaticamente foto, documenti, audio e video."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Percorso file assoluto o relativo al workspace.",
            },
            "caption": {
                "type": "string",
                "description": "Didascalia opzionale.",
            },
            "force_document": {
                "type": "boolean",
                "description": "Se true invia sempre come documento.",
            },
        },
        "required": ["path"],
    },
    handler=notify_file_handler,
    timeout=60,
)

NOTIFY_PHOTO_TOOL = Tool(
    name="notify_photo",
    description="Invia una foto su Telegram.",
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Percorso immagine assoluto o relativo al workspace.",
            },
            "caption": {
                "type": "string",
                "description": "Didascalia opzionale.",
            },
        },
        "required": ["path"],
    },
    handler=notify_photo_handler,
    timeout=60,
)

NOTIFY_POLL_TOOL = Tool(
    name="notify_poll",
    description=(
        "Invia un sondaggio nativo Telegram (sendPoll API). "
        "Mostra il sondaggio come card cliccabile con barre di voto. "
        "chat_id opzionale (default: ultima chat nota). 2-10 opzioni, 1-100 char l'una. "
        "is_anonymous True di default. allows_multiple_answers False di default."
    ),
    parameters={
        "type": "object",
        "properties": {
            "chat_id": {
                "type": "string",
                "description": "ID della chat (numero negativo per gruppi, es. '-100...'). Vuoto = ultima chat nota o primo utente consentito.",
            },
            "question": {
                "type": "string",
                "description": "Domanda del sondaggio (1-300 caratteri).",
            },
            "options": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Lista di 2-10 opzioni (1-100 caratteri ciascuna).",
            },
            "is_anonymous": {
                "type": "boolean",
                "description": "Se True i voti sono anonimi (default True).",
            },
            "allows_multiple_answers": {
                "type": "boolean",
                "description": "Se True si possono scegliere più opzioni (default False).",
            },
        },
        "required": ["question", "options"],
    },
    handler=notify_poll_handler,
    timeout=20,
)
