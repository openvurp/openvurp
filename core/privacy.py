"""
openvurp Core — Privacy Router

Routing per sensibilità: ciò che è privato non lascia la macchina.
La garanzia è del runtime, non una promessa di prompt.

Modalità (PRIVACY_MODE):
- "off"    (default) nessun routing, comportamento invariato
- "strict" le sessioni private principali (quelle in cui viene iniettata
           MEMORY.md e la memoria personale) girano SEMPRE su un modello
           locale quando il backend principale è cloud
- "auto"   route locale solo quando l'input sembra contenere materiale
           sensibile (credenziali, salute, denaro, identificativi)
"""

from __future__ import annotations

import re
from dataclasses import dataclass


# Pattern di contenuto sensibile per la modalità "auto"
_SENSITIVE_PATTERNS = [
    r"\bpassword\b", r"\bpassphrase\b", r"\bapi[_ ]?key\b", r"\btoken\b",
    r"\bsegret[oi]\b", r"\bcredenzial[ei]\b", r"\bsecret\b",
    r"\biban\b", r"\bcodice fiscale\b", r"\bcarta di credito\b",
    r"\bcredit card\b", r"\bcvv\b", r"\bbanca\b", r"\bbank account\b",
    r"\bsalute\b", r"\bmedic[oa]\b", r"\bdiagnos\w+", r"\bterapia\b",
    r"\bstipendio\b", r"\bsalary\b", r"\bdivorzio\b", r"\bavvocato\b",
    r"\bprivat[oa]\b", r"\briservat[oa]\b", r"\bconfidenzial\w+",
]
_SENSITIVE_RE = re.compile("|".join(_SENSITIVE_PATTERNS), re.IGNORECASE)

_CLOUD_BACKENDS = {"openai", "anthropic", "groq", "openai_compatible"}


@dataclass
class PrivacyDecision:
    route_local: bool
    reason: str = ""


def backend_is_cloud(backend: str, model: str = "") -> bool:
    backend = (backend or "").strip().lower()
    if backend in _CLOUD_BACKENDS:
        return True
    # Ollama con modelli ":cloud" esegue comunque su server remoti
    return "cloud" in (model or "").lower()


def looks_sensitive(text: str) -> bool:
    return bool(_SENSITIVE_RE.search(text or ""))


def decide(mode: str, session_type: str, user_input: str,
           main_backend: str, main_model: str = "") -> PrivacyDecision:
    """Decide se il turno deve girare sul modello locale.

    Il routing scatta solo se il backend principale è cloud: se già
    giri in locale non c'è nulla da proteggere.
    """
    mode = (mode or "off").strip().lower()
    if mode not in ("strict", "auto"):
        return PrivacyDecision(False)

    if not backend_is_cloud(main_backend, main_model):
        return PrivacyDecision(False, "backend principale già locale")

    if mode == "strict":
        if session_type == "main":
            return PrivacyDecision(
                True,
                "sessione privata principale: la memoria personale non lascia la macchina",
            )
        if looks_sensitive(user_input):
            return PrivacyDecision(True, "contenuto sensibile rilevato")
        return PrivacyDecision(False)

    # auto
    if looks_sensitive(user_input):
        return PrivacyDecision(True, "contenuto sensibile rilevato")
    return PrivacyDecision(False)


def resolve_local_model() -> tuple[str, str]:
    """Risolvi (backend, model) locale per il routing privacy.

    Ordine: config esplicita → modello principale se già locale →
    modello locale piccolo scoperto via Ollama. ("", "") se non c'è
    nulla di locale disponibile.
    """
    import config as cfg

    backend = str(getattr(cfg, "PRIVACY_LOCAL_BACKEND", "") or "ollama").strip().lower()
    model = str(getattr(cfg, "PRIVACY_LOCAL_MODEL", "") or "").strip()
    if model:
        return backend, model

    main_backend = str(getattr(cfg, "LLM_BACKEND", "") or "").strip().lower()
    main_model = str(getattr(cfg, "LLM_MODEL", "") or "").strip()
    if main_backend == "ollama" and main_model and "cloud" not in main_model.lower():
        return "ollama", main_model

    try:
        from core.model_router import choose_small_local_ollama_model
        local = choose_small_local_ollama_model()
        if local:
            return "ollama", local
    except Exception:
        pass

    return "", ""
