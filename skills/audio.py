# skills/audio.py
# openvurp Audio - Trascrizione audio con Whisper

import whisper
from pathlib import Path
import json

# Config
MODEL_SIZE = "base"  # tiny, base, small, medium, large
AUDIO_DIR = Path(__file__).parent.parent / "memory" / "audio"
AUDIO_DIR.mkdir(parents=True, exist_ok=True)

# Cache modelli
_model = None

def get_model():
    global _model
    if _model is None:
        _model = whisper.load_model(MODEL_SIZE)
    return _model

def transcribe(audio_path: str, language: str = "it") -> dict:
    """Trascrive un file audio. Ritorna dict con testo e metadati."""
    path = Path(audio_path)
    
    if not path.exists():
        return {"success": False, "error": f"File non trovato: {audio_path}"}
    
    try:
        model = get_model()
        result = model.transcribe(str(path), language=language)
        
        return {
            "success": True,
            "text": result["text"].strip(),
            "language": result.get("language", language),
            "segments": len(result.get("segments", [])),
            "audio_path": str(path)
        }
    except Exception as e:
        return {"success": False, "error": str(e)}

if __name__ == "__main__":
    import sys
    
    if len(sys.argv) > 1:
        audio_file = sys.argv[1]
        lang = sys.argv[2] if len(sys.argv) > 2 else "it"
        
        print(f"Trascrivo: {audio_file}")
        result = transcribe(audio_file, lang)
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print("Uso: python skills/audio.py <file_audio> [lingua]")