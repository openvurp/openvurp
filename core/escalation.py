"""
openvurp Core — Giudizio sul proprio cervello

Davanti a una domanda, l'agente si chiede: quanto vale pensarci bene?
Le domande che contano (decisioni, soldi, architettura, sicurezza,
"secondo te" su cose importanti) meritano il modello profondo; la
chiacchiera no. La scelta è del runtime, turno per turno — come la
privacy, è una garanzia meccanica, non una speranza nel prompt.

ESCALATION_MODE:
- off  → mai (default)
- auto → il runtime decide con euristiche a costo zero

Serve un modello profondo configurato (ESCALATION_DEEP_BACKEND/MODEL),
altrimenti resta tutto sul modello principale. Il budget giornaliero
(DAILY_LLM_BUDGET) vale anche per le chiamate profonde.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class EffortDecision:
    effort: str          # "fast" | "normal" | "deep"
    route_deep: bool
    reason: str = ""


# Segnali che la domanda merita il modello profondo: decisioni con
# conseguenze, giudizio richiesto esplicitamente, ambiti delicati.
_DEEP_PATTERNS = [
    (r"\b(secondo te|che ne pensi|consigli|conviene|meglio se|pro e contro)\b",
     "mi viene chiesto un giudizio"),
    (r"\b(architettur|progett\w+ (il|un|lo)|design[ao]?\b|refactor)\w*",
     "scelta di architettura"),
    (r"\b(sicurezz|vulnerabil|exploit|crittograf|password|credenzial)\w*",
     "tema di sicurezza"),
    (r"\b(soldi|investim|contratt|legale|fiscal|tasse|mutuo|stipendio)\w*",
     "conseguenze economiche o legali"),
    (r"\b(important|delicat|critic|irreversibil|definitiv)\w*",
     "l'owner lo segnala come importante"),
    (r"\b(decidere|decisione|scelta difficile|dilemma)\b",
     "c'è una decisione da prendere"),
    (r"\b(analizza|valuta|confronta|rivedi|review)\b.{0,40}\b(tutto|approfondit|attent|bene)\w*",
     "analisi approfondita richiesta"),
    (r"pensaci bene|riflettici|prenditi il tempo|con calma e bene",
     "l'owner chiede di pensarci bene"),
]

_FAST_PATTERNS = [
    r"^\s*(ciao|ehi|hey|ok|grazie|buongiorno|buonanotte|come va)\b",
    r"^\s*(s[iì]|no|va bene|perfetto|ottimo)\s*[.!]?\s*$",
]


def decide_effort(user_input: str) -> EffortDecision:
    """Euristica a costo zero: nessuna chiamata LLM per decidere."""
    text = (user_input or "").strip()
    low = text.lower()

    if not text or len(text) < 12:
        return EffortDecision("fast", False, "messaggio breve")
    for pattern in _FAST_PATTERNS:
        if re.search(pattern, low):
            return EffortDecision("fast", False, "chiacchiera")

    for pattern, reason in _DEEP_PATTERNS:
        if re.search(pattern, low):
            return EffortDecision("deep", True, reason)

    # Domande lunghe e articolate: probabilmente meritano profondità
    if len(text) > 700 and "?" in text:
        return EffortDecision("deep", True, "domanda lunga e articolata")

    return EffortDecision("normal", False, "")


def resolve_deep_model() -> tuple[str, str]:
    """(backend, model) del modello profondo, o ("", "") se non configurato."""
    try:
        import config as cfg
        backend = str(getattr(cfg, "ESCALATION_DEEP_BACKEND", "") or "").strip()
        model = str(getattr(cfg, "ESCALATION_DEEP_MODEL", "") or "").strip()
        if backend and model:
            return backend, model
    except Exception:
        pass
    return "", ""


def escalation_mode() -> str:
    try:
        import config as cfg
        mode = str(getattr(cfg, "ESCALATION_MODE", "off") or "off").strip().lower()
        return mode if mode in ("off", "auto") else "off"
    except Exception:
        return "off"
