# skills/vision.py
# openvurp Vision - Occhi per vedere

import cv2
import requests
import base64
import json
from datetime import datetime
from pathlib import Path

# Config
CAMERA_INDEX = 0  # ELECOM webcam
VISION_MODEL = "qwen3-vl:235b-instruct-cloud"
OLLAMA_API = "http://localhost:11434/api/generate"
CAPTURE_DIR = Path(__file__).parent.parent / "memory" / "captures"
CAPTURE_DIR.mkdir(parents=True, exist_ok=True)

def capture_frame(save: bool = True) -> tuple:
    """Cattura un frame dalla webcam. Ritorna (path, timestamp) o (None, None)."""
    cap = cv2.VideoCapture(CAMERA_INDEX)
    
    if not cap.isOpened():
        return None, None
    
    # Scarta i primi frame (autofocus/esposizione)
    for _ in range(5):
        cap.read()
    
    ret, frame = cap.read()
    cap.release()
    
    if not ret:
        return None, None
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"capture_{timestamp}.jpg"
    filepath = CAPTURE_DIR / filename
    
    if save:
        cv2.imwrite(str(filepath), frame)
        return str(filepath), timestamp
    
    return None, timestamp

def analyze_image(image_path: str, prompt: str = "Descrivi cosa vedi in questa immagine.") -> str:
    """Analizza un'immagine con Ollama vision model (cloud)."""
    try:
        with open(image_path, 'rb') as f:
            img_b64 = base64.b64encode(f.read()).decode('utf-8')
        
        payload = {
            'model': VISION_MODEL,
            'prompt': prompt,
            'images': [img_b64],
            'stream': False
        }
        
        response = requests.post(OLLAMA_API, json=payload, timeout=120)
        result = response.json()
        
        if 'response' in result:
            return result['response']
        elif 'error' in result:
            return f"Errore modello: {result['error']}"
        else:
            return str(result)
            
    except requests.Timeout:
        return "Timeout: l'analisi ha impiegato troppo tempo."
    except Exception as e:
        return f"Errore: {str(e)}"

def see(prompt: str = "Cosa vedi?") -> dict:
    """Cattura e analizza. Ritorna dict con path, timestamp, analisi."""
    path, timestamp = capture_frame()
    
    if path is None:
        return {"success": False, "error": "Impossibile catturare dalla webcam"}
    
    analysis = analyze_image(path, prompt)
    
    return {
        "success": True,
        "image_path": path,
        "timestamp": timestamp,
        "analysis": analysis
    }

def analyze_file(image_path: str, prompt: str = "Descrivi cosa vedi.") -> dict:
    """Analizza un file immagine esistente."""
    if not Path(image_path).exists():
        return {"success": False, "error": f"File non trovato: {image_path}"}
    
    analysis = analyze_image(image_path, prompt)
    return {
        "success": True,
        "image_path": str(image_path),
        "analysis": analysis
    }

if __name__ == "__main__":
    import sys
    
    if len(sys.argv) > 1:
        # Analizza file esistente
        img_path = sys.argv[1]
        prompt = sys.argv[2] if len(sys.argv) > 2 else "Descrivi cosa vedi."
        print(f"Analizzo: {img_path}")
        result = analyze_file(img_path, prompt)
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        # Cattura dalla webcam
        print("Catturo frame...")
        result = see("Cosa vedi? Rispondi in italiano.")
        print(json.dumps(result, indent=2, ensure_ascii=False))