"""
openvurp Tools — Voice (TTS + Microfono)

Permette all'agente di parlare e ascoltare.
"""

from __future__ import annotations

from core.tools import Tool, ToolResult, ErrorType, RetryPolicy


def _config_bool(name: str, default: bool = False) -> bool:
    try:
        import config as cfg
        value = getattr(cfg, name, default)
    except Exception:
        value = default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on", "si", "sì"}


def speak_handler(text: str, voice: str = "", play: bool = True) -> ToolResult:
    """Sintetizza testo in voce e lo riproduce."""
    if not text.strip():
        return ToolResult.fail("No text to synthesize")
    if not _config_bool("VOICE_ENABLED", False):
        return ToolResult.fail(
            "Voce disattivata da configurazione (VOICE_ENABLED=0).",
            error_type=ErrorType.PERMISSION,
        )

    try:
        from voice import speak
        path = speak(text, voice=voice or None, play=play)
        return ToolResult.ok(f"Audio generato: {path}" + (" (riprodotto)" if play else ""))
    except ImportError:
        return ToolResult.fail(
            "edge-tts non installato. Installa con: pip install edge-tts",
            error_type=ErrorType.DEPENDENCY,
        )
    except Exception as e:
        return ToolResult.fail(f"Voice error: {e}")


def listen_mic_handler(duration: float = 5.0, language: str = "it") -> ToolResult:
    """Registra dal microfono e trascrive con Whisper."""
    if not _config_bool("MIC_ENABLED", False):
        return ToolResult.fail(
            "Microfono disattivato da configurazione (MIC_ENABLED=0).",
            error_type=ErrorType.PERMISSION,
        )

    try:
        from voice import listen_microphone
        text = listen_microphone(duration=duration, language=language)
        if text.strip():
            return ToolResult.ok(f"Trascrizione: {text}")
        else:
            return ToolResult.ok("No audio detected.")
    except ImportError as e:
        missing = str(e)
        return ToolResult.fail(
            f"Dipendenza mancante: {missing}. Installa con: pip install sounddevice openai-whisper",
            error_type=ErrorType.DEPENDENCY,
        )
    except Exception as e:
        return ToolResult.fail(f"Microphone error: {e}")


def list_voices_handler(language: str = "it") -> ToolResult:
    """Lista le voci disponibili per una lingua."""
    try:
        from voice import list_voices
        voices = list_voices(language)
        if voices:
            return ToolResult.ok("Voci disponibili:\n" + "\n".join(f"  - {v}" for v in voices))
        return ToolResult.ok(f"Nessuna voce trovata per '{language}'")
    except ImportError:
        return ToolResult.fail("edge-tts non installato")
    except Exception as e:
        return ToolResult.fail(f"Error: {e}")


SPEAK_TOOL = Tool(
    name="speak",
    description="Parla — sintetizza testo in voce e lo riproduce. Usa per comunicare a voce con l'utente.",
    parameters={
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "Testo da dire a voce"},
            "voice": {"type": "string", "description": "Voce edge-tts (opzionale, es: it-IT-DiegoNeural)"},
        },
        "required": ["text"],
    },
    handler=speak_handler,
    timeout=30,
    retry_policy=RetryPolicy(max_retries=1),
)

LISTEN_MIC_TOOL = Tool(
    name="listen_mic",
    description="Ascolta — registra dal microfono e trascrive. Usa per sentire la voce dell'utente.",
    parameters={
        "type": "object",
        "properties": {
            "duration": {"type": "number", "description": "Durata registrazione in secondi (default: 5)"},
            "language": {"type": "string", "description": "Lingua (default: it)"},
        },
        "required": [],
    },
    handler=listen_mic_handler,
    timeout=30,
)

LIST_VOICES_TOOL = Tool(
    name="list_voices",
    description="Lista le voci TTS disponibili per una lingua",
    parameters={
        "type": "object",
        "properties": {
            "language": {"type": "string", "description": "Codice lingua (default: it)"},
        },
        "required": [],
    },
    handler=list_voices_handler,
    timeout=15,
)
