"""
openvurp Voice — Voce dell'agente (TTS + input microfono)

TTS: edge-tts (Microsoft Edge, gratuito, alta qualità)
Input: sounddevice + Whisper (registra dal microfono e trascrive)
"""

import asyncio
import os
import tempfile
import time
from pathlib import Path

OPENVURP_DIR = os.path.dirname(os.path.abspath(__file__))
MEDIA_DIR = os.path.join(OPENVURP_DIR, "memory", "media")

# Voce di default — italiana, femminile
# Altre voci IT: it-IT-DiegoNeural (maschile), it-IT-ElsaNeural (femminile)
# Lista completa: edge-tts --list-voices
DEFAULT_VOICE = "it-IT-DiegoNeural"
DEFAULT_RATE = "+0%"  # velocità: -50% a +100%

# Configurazione microfono
RECORD_SAMPLERATE = 16000
RECORD_CHANNELS = 1


def _get_config(key: str, default):
    """Legge un valore da config.py con fallback."""
    try:
        import config as cfg
        return getattr(cfg, key, default)
    except Exception:
        return default


def _get_config_str(key: str, default: str) -> str:
    value = _get_config(key, default)
    return str(value or default)


def _get_config_bool(key: str, default: bool = False) -> bool:
    value = _get_config(key, default)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on", "si", "sì"}


# ── TTS (Text-to-Speech) ──

def speak(text: str, voice: str = None, rate: str = None,
          output_path: str = None, play: bool = True) -> str:
    """Genera audio da testo e opzionalmente lo riproduce.

    Args:
        text: Testo da sintetizzare
        voice: Voce edge-tts (default: it-IT-DiegoNeural)
        rate: Velocità (es. "+20%", "-10%")
        output_path: Path di output. Se None, usa un file temporaneo.
        play: Se True, riproduce l'audio dopo la generazione.

    Returns:
        Path del file audio generato.
    """
    if not _get_config_bool("VOICE_ENABLED", False):
        raise RuntimeError("Voice output disabled by config (VOICE_ENABLED=0)")

    voice = voice or _get_config_str("VOICE_NAME", DEFAULT_VOICE)
    rate = rate or _get_config_str("VOICE_RATE", DEFAULT_RATE)

    if not output_path:
        os.makedirs(MEDIA_DIR, exist_ok=True)
        output_path = os.path.join(MEDIA_DIR, f"voice_{int(time.time())}.mp3")

    # Genera audio con edge-tts
    asyncio.run(_generate_tts(text, voice, rate, output_path))

    if play:
        play_audio(output_path)

    return output_path


async def _generate_tts(text: str, voice: str, rate: str, output_path: str):
    """Genera audio con edge-tts (async)."""
    import edge_tts
    communicate = edge_tts.Communicate(text, voice, rate=rate)
    await communicate.save(output_path)


def play_audio(path: str):
    """Riproduce un file audio con pygame."""
    if not os.path.exists(path):
        return

    try:
        import pygame
        pygame.mixer.init()
        pygame.mixer.music.load(path)
        pygame.mixer.music.play()
        # Attendi che l'audio finisca
        import time
        while pygame.mixer.music.get_busy():
            time.sleep(0.05)
        pygame.mixer.quit()
    except Exception:
        # Fallback: apri con sistema
        import subprocess
        import platform
        system = platform.system()
        if system == "Windows":
            subprocess.Popen(["cmd", "/c", "start", "", path],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif system == "Darwin":
            subprocess.run(["afplay", path], timeout=60)


# ── STT (Speech-to-Text) via microfono ──

def listen_microphone(duration: float = 5.0, model: str = None,
                      language: str = "it") -> str:
    """Registra dal microfono e trascrive con Whisper.

    Args:
        duration: Durata registrazione in secondi
        model: Modello Whisper (default: da config o "base")
        language: Lingua per Whisper

    Returns:
        Testo trascritto
    """
    if not _get_config_bool("MIC_ENABLED", False):
        raise RuntimeError("Microphone input disabled by config (MIC_ENABLED=0)")

    import sounddevice as sd
    import numpy as np

    model = model or _get_config_str("AUDIO_MODEL", "base")

    # Registra
    print(f"  Ascoltando per {duration}s...")
    audio = sd.rec(
        int(duration * RECORD_SAMPLERATE),
        samplerate=RECORD_SAMPLERATE,
        channels=RECORD_CHANNELS,
        dtype="float32",
    )
    sd.wait()
    print("  Trascrivo...")

    # Salva in wav temporaneo
    tmp_path = os.path.join(tempfile.gettempdir(), f"openvurp_mic_{int(time.time())}.wav")
    _save_wav(tmp_path, audio, RECORD_SAMPLERATE)

    # Trascrivi con Whisper
    try:
        from ears import listen
        text = listen(tmp_path, model=model, language=language)
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass

    return text


def _save_wav(path: str, audio, samplerate: int):
    """Salva array numpy come file WAV."""
    import wave
    import struct
    import numpy as np

    audio_int16 = (audio * 32767).astype(np.int16)
    with wave.open(path, "w") as wf:
        wf.setnchannels(RECORD_CHANNELS)
        wf.setsampwidth(2)  # 16-bit
        wf.setframerate(samplerate)
        wf.writeframes(audio_int16.tobytes())


# ── Utility ──

def list_voices(language: str = "it") -> list[str]:
    """Lista le voci edge-tts disponibili per una lingua."""
    voices = asyncio.run(_list_voices_async())
    return [v for v in voices if language.lower() in v.lower()]


async def _list_voices_async() -> list[str]:
    import edge_tts
    voices = await edge_tts.list_voices()
    return [f"{v['ShortName']} — {v['Gender']}, {v['Locale']}" for v in voices]

# ── CLI ──

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Uso:")
        print("  python voice.py speak 'testo da dire'")
        print("  python voice.py listen [durata_secondi]")
        print("  python voice.py voices [lingua]")
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "speak":
        text = " ".join(sys.argv[2:]) or "Ciao, sono openvurp."
        path = speak(text)
        print(f"Audio: {path}")

    elif cmd == "listen":
        duration = float(sys.argv[2]) if len(sys.argv) > 2 else 5.0
        text = listen_microphone(duration=duration)
        print(f"Hai detto: {text}")

    elif cmd == "voices":
        lang = sys.argv[2] if len(sys.argv) > 2 else "it"
        for v in list_voices(lang):
            print(f"  {v}")
