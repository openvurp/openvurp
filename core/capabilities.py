"""
openvurp Core — Runtime Capabilities

Rende esplicite le capacità reali dell'agente, derivate da tool registrati,
configurazione e dipendenze disponibili. Serve a evitare drift tra prompt,
canali e wiring effettivo.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import importlib.util
from typing import Iterable


@dataclass
class CapabilityReport:
    vision_tool: bool = False
    vision_model: str = ""
    desktop_capture_tool: bool = False
    browser_tool: bool = False
    browser_devtools_tool: bool = False
    audio_file_tool: bool = False
    audio_model: str = ""
    audio_enabled: bool = True
    audio_transcribe_enabled: bool = True
    pdf_tool: bool = False
    microphone_tool: bool = False
    tts_tool: bool = False
    microphone_enabled: bool = False
    voice_enabled: bool = False
    voice_tools_enabled: bool = False
    notify_file_tool: bool = False
    notify_photo_tool: bool = False
    whisper_available: bool = False
    sounddevice_available: bool = False
    edge_tts_available: bool = False
    warnings: list[str] = field(default_factory=list)


def _module_available(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def inspect_runtime_capabilities(tool_names: Iterable[str]) -> CapabilityReport:
    """Costruisce un report capability partendo dai tool registrati."""
    tools = set(tool_names)
    try:
        import config as cfg
    except Exception:
        cfg = None
    audio_enabled = bool(getattr(cfg, "AUDIO_ENABLED", True)) if cfg else True
    audio_transcribe_enabled = bool(
        getattr(cfg, "AUDIO_TRANSCRIBE_ENABLED", audio_enabled)
    ) if cfg else True
    voice_enabled = bool(getattr(cfg, "VOICE_ENABLED", False)) if cfg else False
    voice_tools_enabled = bool(
        getattr(cfg, "VOICE_TOOLS_ENABLED", voice_enabled)
    ) if cfg else False
    microphone_enabled = bool(getattr(cfg, "MIC_ENABLED", voice_tools_enabled)) if cfg else False

    report = CapabilityReport(
        vision_tool="image_analyze" in tools,
        vision_model=getattr(cfg, "VISION_MODEL", "") if cfg else "",
        desktop_capture_tool="desktop_screenshot" in tools,
        browser_tool="browser" in tools,
        browser_devtools_tool="browser_devtools" in tools,
        audio_file_tool="audio_transcribe" in tools and audio_enabled and audio_transcribe_enabled,
        audio_model=getattr(cfg, "AUDIO_MODEL", "") if cfg else "",
        audio_enabled=audio_enabled,
        audio_transcribe_enabled=audio_transcribe_enabled,
        pdf_tool="pdf_read" in tools,
        microphone_tool="listen_mic" in tools and microphone_enabled,
        tts_tool="speak" in tools and voice_enabled and voice_tools_enabled,
        microphone_enabled=microphone_enabled,
        voice_enabled=voice_enabled,
        voice_tools_enabled=voice_tools_enabled,
        notify_file_tool="notify_file" in tools,
        notify_photo_tool="notify_photo" in tools,
        whisper_available=_module_available("whisper"),
        sounddevice_available=_module_available("sounddevice"),
        edge_tts_available=_module_available("edge_tts"),
    )

    if report.vision_model and not report.vision_tool:
        report.warnings.append(
            "VISION_MODEL è configurato ma il tool image_analyze non è registrato."
        )
    if report.audio_model and "audio_transcribe" not in tools and report.audio_enabled:
        report.warnings.append(
            "AUDIO_MODEL è configurato ma il tool audio_transcribe non è registrato."
        )
    if report.audio_file_tool and not report.whisper_available:
        report.warnings.append(
            "audio_transcribe è registrato ma Whisper non è installato."
        )
    if report.microphone_tool and not report.whisper_available:
        report.warnings.append(
            "listen_mic è registrato ma Whisper non è installato."
        )
    if report.microphone_tool and not report.sounddevice_available:
        report.warnings.append(
            "listen_mic è registrato ma sounddevice non è installato."
        )
    if report.tts_tool and not report.edge_tts_available:
        report.warnings.append(
            "speak è registrato ma edge-tts non è installato."
        )

    return report


def render_capability_prompt(report: CapabilityReport) -> str:
    """Rende il report in una sezione compatta per il system prompt."""
    lines = ["## SENSORI REALI"]

    # Gerarchia web — evita il drift "uso devtools per cercare sul web"
    lines.append(
        "- Ricerca web: parti SEMPRE da `web_search` per trovare URL/fatti/documentazione. "
        "Poi `web_fetch` per leggere il contenuto di un link. "
        "`browser` e `browser_devtools` NON sono tool di ricerca: servono a "
        "interagire/debuggare un sito, non a trovarlo."
    )

    if report.vision_tool:
        model = report.vision_model or "vision model non configurato"
        lines.append(f"- Immagini e screenshot: usa `image_analyze` con modello `{model}`.")
    else:
        lines.append("- Immagini e screenshot: non disponibili in questo turno.")

    if report.desktop_capture_tool:
        lines.append("- Screenshot desktop locale: usa `desktop_screenshot`, poi analizza il file con `image_analyze` se serve.")

    if report.browser_tool:
        lines.append(
            "- Browser web: usa `browser` come tool primario. `mode=\"shared\"` riusa Chrome reale quando pronto; "
            "`mode=\"isolated\"` usa un browser controllato separato e supporta engine `chromium`, `firefox` e `webkit`. "
            "Per Chromium sono disponibili anche i channel `chromium`, `chrome`, `chrome-beta`, `chrome-dev`, `chrome-canary`, "
            "`msedge`, `msedge-beta`, `msedge-dev`, `msedge-canary`. "
            "Se il task riguarda una tab aperta o un sito già loggato, preferisci `mode=\"shared\"`. "
            "Se serve preparare il browser condiviso, usa `browser` con `action=\"status\"` o `action=\"relaunch\"`."
        )

    if report.browser_devtools_tool:
        lines.append(
            "- Debugging avanzato Chrome (DOM live, console, network, screenshot): usa `browser_devtools` "
            "SOLO per ispezionare/debuggare pagine gia' aperte. "
            "Non e' un motore di ricerca ne' un lettore di URL."
        )

    if not report.audio_enabled or not report.audio_transcribe_enabled:
        lines.append("- Audio e vocali da file: disattivati da configurazione.")
    elif report.audio_file_tool:
        model = report.audio_model or "audio model non configurato"
        suffix = "" if report.whisper_available else " Whisper al momento manca e il tool può fallire."
        lines.append(
            f"- Audio e vocali da file: usa `audio_transcribe` con modello `{model}`.{suffix}"
        )
    else:
        lines.append("- Audio e vocali da file: non disponibili in questo turno.")

    if not report.microphone_enabled:
        lines.append("- Microfono live: disattivato da configurazione.")
    elif report.microphone_tool:
        deps = []
        if not report.whisper_available:
            deps.append("Whisper mancante")
        if not report.sounddevice_available:
            deps.append("sounddevice mancante")
        if deps:
            lines.append(f"- Microfono live: tool `listen_mic` presente ma inattivo ({', '.join(deps)}).")
        else:
            lines.append("- Microfono live: usa `listen_mic`.")
    else:
        lines.append("- Microfono live: non disponibile in questo turno.")

    if not report.voice_enabled or not report.voice_tools_enabled:
        lines.append("- Voce in uscita: disattivata da configurazione.")
    elif report.tts_tool:
        if report.edge_tts_available:
            lines.append("- Voce in uscita: usa `speak`.")
        else:
            lines.append("- Voce in uscita: tool `speak` presente ma edge-tts manca.")
    else:
        lines.append("- Voce in uscita: non disponibile in questo turno.")

    if report.pdf_tool:
        lines.append("- PDF: usa `pdf_read`.")

    if report.notify_file_tool or report.notify_photo_tool:
        available = []
        if report.notify_photo_tool:
            available.append("`notify_photo`")
        if report.notify_file_tool:
            available.append("`notify_file`")
        lines.append(f"- Invio media/file all'utente: usa {', '.join(available)}.")

    lines.append(
        "- Importante: il modello chat non riceve automaticamente immagini o audio raw. "
        "Quando arrivano come file o allegati, devi usare i tool sopra."
    )
    lines.append(
        "- Queste sono le capacità già cablate. Se te ne serve una vicina ma qui manca, "
        "non fingerla: prova a costruirla con i tool generici o creando/ricaricando un plugin; "
        "se non è sicuro o fattibile, chiedi."
    )

    return "\n".join(lines)
