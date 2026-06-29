"""
openvurp Tools — Media Analysis (Image + PDF)

Analisi immagini, audio e PDF.
Import condizionale: funziona anche senza dipendenze opzionali.
"""

from __future__ import annotations

import os
import base64
from core.tools import Tool, ToolResult, ErrorType, RetryPolicy


def _load_media_backend():
    """Legge backend e modelli media dalla configurazione."""
    try:
        import config as cfg
    except Exception:
        cfg = None

    backend = getattr(cfg, "LLM_BACKEND", "ollama")
    vision_model = getattr(cfg, "VISION_MODEL", "") or getattr(cfg, "LLM_MODEL", "") or "llava"
    base_url = (
        getattr(cfg, "VISION_BASE_URL", "")
        or getattr(cfg, "LLM_BASE_URL", "")
        or "http://localhost:11434"
    )
    return cfg, backend, vision_model, base_url


# ── Image Analysis ──

def image_analyze_handler(path: str, prompt: str = "Descrivi questa immagine in dettaglio") -> ToolResult:
    """Analizza un'immagine usando un modello vision."""
    if not os.path.exists(path):
        return ToolResult.fail(f"File non trovato: {path}", error_type=ErrorType.NOT_FOUND)

    ext = os.path.splitext(path)[1].lower()
    if ext not in ('.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp'):
        return ToolResult.fail(f"Formato immagine non supportato: {ext}")

    try:
        with open(path, "rb") as f:
            image_data = f.read()
    except Exception as e:
        return ToolResult.fail(f"File read error: {e}")

    b64 = base64.b64encode(image_data).decode("utf-8")
    mime_types = {
        '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
        '.gif': 'image/gif', '.webp': 'image/webp', '.bmp': 'image/bmp',
    }
    mime = mime_types.get(ext, 'image/png')

    cfg, backend, model, base_url = _load_media_backend()

    try:
        if backend == "ollama":
            return _analyze_ollama(model, b64, prompt, base_url)
        elif backend == "anthropic":
            return _analyze_anthropic(model, b64, mime, prompt, cfg)
        elif backend in ("openai", "openai_compatible"):
            return _analyze_openai(model, b64, mime, prompt, cfg)
        else:
            return ToolResult.fail(f"Backend {backend} non supporta vision")
    except Exception as e:
        return ToolResult.fail(f"Image analysis error: {e}")


def _analyze_ollama(model: str, b64: str, prompt: str, base_url: str) -> ToolResult:
    import requests
    r = requests.post(f"{base_url}/api/chat", json={
        "model": model,
        "messages": [{"role": "user", "content": prompt, "images": [b64]}],
        "stream": False,
    }, timeout=120)
    r.raise_for_status()
    return ToolResult.ok(r.json()["message"]["content"])


def _analyze_anthropic(model: str, b64: str, mime: str, prompt: str, cfg) -> ToolResult:
    import anthropic
    client = anthropic.Anthropic(api_key=getattr(cfg, 'LLM_API_KEY', None))
    r = client.messages.create(
        model=model, max_tokens=2048,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": mime, "data": b64}},
                {"type": "text", "text": prompt},
            ]
        }]
    )
    return ToolResult.ok(r.content[0].text)


def _analyze_openai(model: str, b64: str, mime: str, prompt: str, cfg) -> ToolResult:
    from openai import OpenAI
    kw = {}
    if hasattr(cfg, 'LLM_API_KEY'):
        kw["api_key"] = cfg.LLM_API_KEY
    if hasattr(cfg, 'LLM_BASE_URL'):
        kw["base_url"] = cfg.LLM_BASE_URL
    client = OpenAI(**kw)
    r = client.chat.completions.create(
        model=model, max_tokens=2048,
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
            ]
        }]
    )
    return ToolResult.ok(r.choices[0].message.content)


# ── Audio Analysis ──

def audio_transcribe_handler(path: str, language: str = "it", translate: bool = False) -> ToolResult:
    """Trascrive un file audio con Whisper."""
    try:
        import config as cfg
        audio_enabled = bool(getattr(cfg, "AUDIO_ENABLED", True))
        transcribe_enabled = bool(getattr(cfg, "AUDIO_TRANSCRIBE_ENABLED", audio_enabled))
    except Exception:
        audio_enabled = True
        transcribe_enabled = True

    if not audio_enabled or not transcribe_enabled:
        return ToolResult.fail(
            "Trascrizione audio disattivata da configurazione "
            "(AUDIO_ENABLED=0 o AUDIO_TRANSCRIBE_ENABLED=0).",
            error_type=ErrorType.PERMISSION,
        )

    if not os.path.exists(path):
        return ToolResult.fail(f"File non trovato: {path}", error_type=ErrorType.NOT_FOUND)

    ext = os.path.splitext(path)[1].lower()
    if ext not in ('.mp3', '.wav', '.m4a', '.flac', '.ogg', '.webm', '.mp4'):
        return ToolResult.fail(f"Formato audio non supportato: {ext}")

    try:
        from ears import transcribe
        result = transcribe(path, language=language or None, translate=translate)
    except ImportError:
        return ToolResult.fail(
            "Whisper non installato. Installa con: pip install faster-whisper "
            "(consigliato, CPU) oppure pip install openai-whisper",
            error_type=ErrorType.DEPENDENCY,
        )
    except Exception as e:
        return ToolResult.fail(f"Audio transcription error: {e}")

    text = result.get("text", "").strip()
    detected_language = result.get("language", language or "unknown")
    if not text:
        return ToolResult.ok("Trascrizione vuota.")

    return ToolResult.ok(
        f"Trascrizione ({detected_language}): {text}"
    )


# ── PDF Analysis ──

def pdf_read_handler(path: str, pages: str = None) -> ToolResult:
    """Legge un file PDF ed estrae il testo."""
    if not os.path.exists(path):
        return ToolResult.fail(f"File non trovato: {path}", error_type=ErrorType.NOT_FOUND)

    if not path.lower().endswith('.pdf'):
        return ToolResult.fail("The file is not a PDF")

    # Parse page range
    page_range = None
    if pages:
        try:
            if '-' in pages:
                start, end = pages.split('-', 1)
                page_range = (int(start) - 1, int(end))  # 0-indexed start
            else:
                p = int(pages) - 1
                page_range = (p, p + 1)
        except ValueError:
            return ToolResult.fail(f"Invalid page range: {pages}")

    # Prova PyMuPDF (fitz)
    try:
        import fitz
        return _read_pdf_fitz(path, page_range)
    except ImportError:
        pass

    # Prova pdfplumber
    try:
        import pdfplumber
        return _read_pdf_plumber(path, page_range)
    except ImportError:
        pass

    # Fallback: pdftotext via subprocess
    try:
        return _read_pdf_subprocess(path, page_range)
    except Exception:
        pass

    return ToolResult.fail(
        "No PDF reader available. Install with:\n"
        "  pip install PyMuPDF   (consigliato)\n"
        "  pip install pdfplumber\n"
        "  oppure installa pdftotext (poppler-utils)"
    )


def _read_pdf_fitz(path: str, page_range: tuple = None) -> ToolResult:
    import fitz
    doc = fitz.open(path)
    total = len(doc)

    if page_range:
        start, end = page_range
        start = max(0, start)
        end = min(total, end)
    else:
        start, end = 0, total

    parts = [f"[PDF: {os.path.basename(path)}, {total} pagine]\n"]
    for i in range(start, end):
        page = doc[i]
        text = page.get_text()
        parts.append(f"\n--- Pagina {i + 1} ---\n{text}")

    doc.close()
    output = "\n".join(parts)
    if len(output) > 20000:
        output = output[:20000] + f"\n[...troncato, testo totale ~{len(output)} chars]"
    return ToolResult.ok(output)


def _read_pdf_plumber(path: str, page_range: tuple = None) -> ToolResult:
    import pdfplumber
    with pdfplumber.open(path) as pdf:
        total = len(pdf.pages)
        if page_range:
            start, end = page_range
            start = max(0, start)
            end = min(total, end)
        else:
            start, end = 0, total

        parts = [f"[PDF: {os.path.basename(path)}, {total} pagine]\n"]
        for i in range(start, end):
            text = pdf.pages[i].extract_text() or ""
            parts.append(f"\n--- Pagina {i + 1} ---\n{text}")

    output = "\n".join(parts)
    if len(output) > 20000:
        output = output[:20000] + f"\n[...troncato]"
    return ToolResult.ok(output)


def _read_pdf_subprocess(path: str, page_range: tuple = None) -> ToolResult:
    import subprocess
    cmd = ["pdftotext", path, "-"]
    if page_range:
        cmd = ["pdftotext", "-f", str(page_range[0] + 1), "-l", str(page_range[1]), path, "-"]

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(f"pdftotext fallito: {result.stderr}")

    output = result.stdout
    if len(output) > 20000:
        output = output[:20000] + "\n[...troncato]"
    return ToolResult.ok(output)


# ── Tool Definitions ──

IMAGE_TOOL = Tool(
    name="image_analyze",
    description="Analizza un'immagine usando il modello vision corrente",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Percorso dell'immagine"},
            "prompt": {"type": "string", "description": "Cosa analizzare nell'immagine (opzionale)"},
        },
        "required": ["path"],
    },
    handler=image_analyze_handler,
    timeout=120,
    retry_policy=RetryPolicy(max_retries=1),
)

AUDIO_TRANSCRIBE_TOOL = Tool(
    name="audio_transcribe",
    description="Trascrive un file audio con Whisper usando il modello audio corrente",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Percorso del file audio"},
            "language": {"type": "string", "description": "Lingua attesa, es. 'it' o 'en' (opzionale)"},
            "translate": {"type": "boolean", "description": "Se true, traduce in inglese"},
        },
        "required": ["path"],
    },
    handler=audio_transcribe_handler,
    timeout=180,
    retry_policy=RetryPolicy(max_retries=1),
)

PDF_TOOL = Tool(
    name="pdf_read",
    description="Legge e estrae testo da un file PDF",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Percorso del file PDF"},
            "pages": {"type": "string", "description": "Range pagine es. '1-5' o '3' (opzionale)"},
        },
        "required": ["path"],
    },
    handler=pdf_read_handler,
    timeout=60,
)
