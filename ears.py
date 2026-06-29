"""
openvurp Ears - Audio Transcription Module

Trascrive audio in testo. Usa faster-whisper (CPU, leggero) quando
disponibile, altrimenti openai-whisper. L'API pubblica è identica
in entrambi i casi: transcribe(), listen(), translate().
"""

from pathlib import Path
import warnings

warnings.filterwarnings("ignore", category=UserWarning)

# Backend disponibili, in ordine di preferenza
_BACKEND = None
try:
    from faster_whisper import WhisperModel as _FasterWhisperModel
    _BACKEND = "faster-whisper"
except ImportError:
    try:
        import whisper as _openai_whisper
        _BACKEND = "whisper"
    except ImportError:
        raise ImportError(
            "Nessun backend Whisper disponibile. Installa con: "
            "pip install faster-whisper (consigliato) oppure pip install openai-whisper"
        )

# Modelli disponibili (dal più veloce al più preciso)
# tiny < base < small < medium < large
MODELS = {
    "tiny": "tiny",        # ~72MB, veloce, base
    "base": "base",        # ~142MB, buon compromesso
    "small": "small",      # ~466MB, più preciso
    "medium": "medium",    # ~1.5GB, molto preciso
    "large": "large-v3",   # ~3GB, il migliore
}

# Modello di default — sovrascrivibile da config.py
try:
    import config as _cfg
    DEFAULT_MODEL = getattr(_cfg, "AUDIO_MODEL", "") or "base"
except Exception:
    DEFAULT_MODEL = "base"

# Cache dei modelli caricati
_loaded_models = {}


def get_model(model_name: str = DEFAULT_MODEL):
    """Carica un modello Whisper (con cache)."""
    model_key = MODELS.get(model_name, model_name)

    if model_key not in _loaded_models:
        print(f"Caricando modello {model_key} ({_BACKEND})...")
        if _BACKEND == "faster-whisper":
            _loaded_models[model_key] = _FasterWhisperModel(
                model_key, device="cpu", compute_type="int8"
            )
        else:
            _loaded_models[model_key] = _openai_whisper.load_model(model_key)

    return _loaded_models[model_key]


def transcribe(
    audio_path: str,
    model: str = DEFAULT_MODEL,
    language: str = None,
    translate: bool = False
) -> dict:
    """
    Trascrive un file audio.

    Args:
        audio_path: Percorso del file audio
        model: Modello Whisper da usare (tiny, base, small, medium, large)
        language: Lingua (es. "it", "en"). None = auto-detect
        translate: Se True, traduce in inglese

    Returns:
        Dict con 'text', 'segments', 'language'
    """
    audio_path = Path(audio_path)

    if not audio_path.exists():
        raise FileNotFoundError(f"Audio non trovato: {audio_path}")

    supported = {"mp3", "wav", "m4a", "flac", "ogg", "webm", "mp4", "oga", "opus"}
    if audio_path.suffix.lower().lstrip(".") not in supported:
        raise ValueError(f"Formato non supportato: {audio_path.suffix}")

    loaded = get_model(model)

    if _BACKEND == "faster-whisper":
        segments_iter, info = loaded.transcribe(
            str(audio_path),
            language=language or None,
            task="translate" if translate else "transcribe",
        )
        segments = [
            {"start": s.start, "end": s.end, "text": s.text}
            for s in segments_iter
        ]
        text = "".join(s["text"] for s in segments).strip()
        return {
            "text": text,
            "segments": segments,
            "language": getattr(info, "language", language or "unknown"),
        }

    options = {}
    if language:
        options["language"] = language
    if translate:
        options["task"] = "translate"
    result = loaded.transcribe(str(audio_path), **options)
    return {
        "text": result["text"].strip(),
        "segments": result.get("segments", []),
        "language": result.get("language", "unknown"),
    }


def listen(
    audio_path: str,
    model: str = DEFAULT_MODEL,
    language: str = None
) -> str:
    """Versione semplificata: ritorna solo il testo."""
    result = transcribe(audio_path, model=model, language=language)
    return result["text"]


def translate(
    audio_path: str,
    model: str = DEFAULT_MODEL
) -> str:
    """Trascrive e traduce in inglese."""
    result = transcribe(audio_path, model=model, translate=True)
    return result["text"]


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Uso: python ears.py <audio_file> [modello]")
        print("Modelli: tiny, base (default), small, medium, large")
        sys.exit(1)

    audio_file = sys.argv[1]
    model = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_MODEL

    print(f"Trascrivendo {audio_file} con modello {model} ({_BACKEND})...")
    text = listen(audio_file, model=model)
    print(f"Testo: {text}")
