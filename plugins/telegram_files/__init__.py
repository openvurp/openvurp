"""Plugin per inviare file su Telegram."""

from __future__ import annotations

import os
import json
import threading
from core.tools import Tool, ToolResult, ErrorType

OPENVURP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TELEGRAM_STATE_PATH = os.path.join(OPENVURP_DIR, "memory", "telegram_state.json")


def _get_telegram():
    """Ottieni token e chat_id da config."""
    try:
        import config as cfg
        token = getattr(cfg, "TELEGRAM_TOKEN", "")
        allowed = getattr(cfg, "TELEGRAM_ALLOWED_USERS", [])
        if not token:
            return None, None
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


def _guess_method(filepath: str) -> str:
    """Individua il metodo API Telegram corretto in base all'estensione."""
    ext = os.path.splitext(filepath)[1].lower()

    # Foto
    if ext in (".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tiff"):
        return "sendPhoto"
    # Video
    if ext in (".mp4", ".mov", ".avi", ".mkv", ".webm", ".gif"):
        return "sendVideo"
    # Audio
    if ext in (".mp3", ".ogg", ".wav", ".flac", ".m4a", ".aac"):
        return "sendAudio"
    # Voice (nota audio Telegram)
    if ext == ".opus":
        return "sendVoice"
    # Documento (PDF, zip, ecc.)
    return "sendDocument"


def _file_field(method: str) -> str:
    """Campo file corretto per ogni metodo API."""
    return {
        "sendPhoto": "photo",
        "sendVideo": "video",
        "sendAudio": "audio",
        "sendVoice": "voice",
        "sendDocument": "document",
    }.get(method, "document")


def _send_file_sync(token: str, chat_id: str, filepath: str, caption: str = "") -> tuple[bool, str]:
    """Invia un file su Telegram (sincrono). Ritorna (successo, messaggio)."""
    import requests

    if not os.path.isfile(filepath):
        return False, f"File non trovato: {filepath}"

    size_mb = os.path.getsize(filepath) / (1024 * 1024)
    if size_mb > 50:
        return False, f"File troppo grande ({size_mb:.1f} MB). Limite Telegram: 50 MB."

    method = _guess_method(filepath)
    url = f"https://api.telegram.org/bot{token}/{method}"
    field = _file_field(method)
    filename = os.path.basename(filepath)

    try:
        with open(filepath, "rb") as f:
            data = {"chat_id": chat_id}
            if caption:
                data["caption"] = caption
            files = {field: (filename, f)}
            r = requests.post(url, data=data, files=files, timeout=60)

        if r.ok:
            result = r.json()
            msg_id = result.get("result", {}).get("message_id", "?")
            return True, f"Inviato! Message ID: {msg_id}"
        else:
            err = r.json().get("description", r.text[:200])
            return False, f"Errore Telegram: {err}"
    except requests.exceptions.Timeout:
        return False, "Timeout â€” file troppo grande o connessione lenta."
    except Exception as e:
        return False, f"Errore invio: {e}"


def send_telegram_file_handler(filepath: str, caption: str = "") -> ToolResult:
    """Invia un file su Telegram (immagine, PDF, documento, audio, video).

    Supporta: immagini (jpg, png, webp), PDF, documenti generici,
    audio (mp3, ogg, wav), video (mp4, mov, avi).
    Limite: 50 MB per Telegram.
    """
    if not filepath.strip():
        return ToolResult.fail("Percorso file vuoto", error_type=ErrorType.VALIDATION)

    # Risolvi path relativo rispetto al workspace
    if not os.path.isabs(filepath):
        filepath = os.path.join(OPENVURP_DIR, filepath)

    filepath = os.path.normpath(filepath)

    if not os.path.isfile(filepath):
        return ToolResult.fail(
            f"File non trovato: {filepath}",
            error_type=ErrorType.NOT_FOUND
        )

    token, chat_id = _get_telegram()
    if not token:
        return ToolResult.fail(
            "Telegram non configurato. Serve TELEGRAM_TOKEN in .env o nell'ambiente",
            error_type=ErrorType.DEPENDENCY,
        )

    size_mb = os.path.getsize(filepath) / (1024 * 1024)
    filename = os.path.basename(filepath)
    method = _guess_method(filepath)

    # Invio in background per non bloccare
    result_holder = {"done": False, "success": False, "msg": ""}

    def _send():
        ok, msg = _send_file_sync(token, chat_id, filepath, caption)
        result_holder["done"] = True
        result_holder["success"] = ok
        result_holder["msg"] = msg

    t = threading.Thread(target=_send, daemon=True, name="tg-send-file")
    t.start()

    # Per file piccoli aspettiamo il risultato; per grandi torniamo subito
    if size_mb < 10:
        t.join(timeout=30)
        if result_holder["done"]:
            if result_holder["success"]:
                return ToolResult.ok(f"ðŸ“„ {filename} ({size_mb:.1f} MB) â†’ Telegram. {result_holder['msg']}")
            else:
                return ToolResult.fail(result_holder["msg"])
        else:
            return ToolResult.ok(f"ðŸ“„ {filename} ({size_mb:.1f} MB) â†’ invio in corso...")
    else:
        return ToolResult.ok(f"ðŸ“„ {filename} ({size_mb:.1f} MB) â†’ invio in background avviato...")


# â”€â”€ Tool definition â”€â”€

SEND_FILE_TOOL = Tool(
    name="send_telegram_file",
    description=(
        "Invia un file su Telegram all'utente. "
        "Supporta immagini (jpg, png, webp), PDF, documenti, audio (mp3, ogg, wav), video (mp4, mov). "
        "Il tipo viene rilevato automaticamente dall'estensione. "
        "Limite: 50 MB. Usa questo tool quando l'utente ti chiede di inviare un file su Telegram."
    ),
    parameters={
        "type": "object",
        "properties": {
            "filepath": {
                "type": "string",
                "description": "Percorso completo del file da inviare (es: C:\\Users\\alice\\Desktop\\image.png)",
            },
            "caption": {
                "type": "string",
                "description": "Didascalia opzionale per il file (max 1024 caratteri)",
            },
        },
        "required": ["filepath"],
    },
    handler=send_telegram_file_handler,
    timeout=60,
)


def register(manager):
    """Registra il tool nel ToolRegistry."""
    manager.register(SEND_FILE_TOOL)


def unregister(manager):
    """Rimuovi il tool dal ToolRegistry."""
    manager.unregister("send_telegram_file")
