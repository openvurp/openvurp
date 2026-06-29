"""
openvurp Security — Egress guard

Controlla cosa esce dalla macchina. Due rischi, dopo un prompt injection:
1. esfiltrazione di segreti via notify/web (messaggi, query, URL)
2. invio dati a domini arbitrari

Difesa:
- blocca qualsiasi azione in uscita che trasporta materiale che somiglia
  a un segreto (riusa i pattern di redazione dell'audit)
- allowlist di domini opzionale (EGRESS_ALLOWLIST): se impostata, web_fetch
  e notify possono raggiungere solo quei domini
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

from core.security.audit import redact

# Tool che mandano dati fuori dalla macchina
EXTERNAL_TEXT_TOOLS = {"notify", "notify_voice", "notify_photo", "notify_file",
                       "schedule_notify"}
EXTERNAL_URL_TOOLS = {"web_fetch"}

# Argomenti che trasportano testo verso l'esterno
_TEXT_ARG_KEYS = ("message", "text", "caption", "body", "content")


def contains_secret(text: str) -> bool:
    """True se il testo contiene qualcosa che somiglia a un segreto.

    Usa la stessa redazione degli audit: se redact() cambia il testo,
    allora conteneva un segreto.
    """
    if not text:
        return False
    return redact(text) != text


def _allowlist() -> list[str]:
    try:
        import config as cfg
        raw = getattr(cfg, "EGRESS_ALLOWLIST", "") or ""
    except Exception:
        raw = ""
    if isinstance(raw, (list, tuple)):
        items = list(raw)
    else:
        items = re.split(r"[,\s]+", str(raw))
    return [d.strip().lower() for d in items if d.strip()]


def _domain_allowed(url: str, allow: list[str]) -> bool:
    if not allow:
        return True
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    if not host:
        return False
    return any(host == d or host.endswith("." + d) for d in allow)


def check_egress(tool_name: str, args: dict) -> tuple[bool, str]:
    """Returns (allowed, reason). reason non vuota = blocco."""
    args = args if isinstance(args, dict) else {}

    # 1. Segreti in uscita: bloccati sempre
    if tool_name in EXTERNAL_TEXT_TOOLS or tool_name in EXTERNAL_URL_TOOLS:
        keys = _TEXT_ARG_KEYS + (("url",) if tool_name in EXTERNAL_URL_TOOLS else ())
        for key in keys:
            if contains_secret(str(args.get(key, "") or "")):
                return False, (
                    f"[EGRESS] '{tool_name}' bloccato: il contenuto in uscita "
                    f"contiene qualcosa che somiglia a un segreto (chiave/token/"
                    f"credenziale). Non esfiltrare dati sensibili."
                )

    # 2. Allowlist domini (se configurata)
    allow = _allowlist()
    if allow:
        if tool_name in EXTERNAL_URL_TOOLS:
            url = str(args.get("url", "") or "")
            if url and not _domain_allowed(url, allow):
                return False, (
                    f"[EGRESS] dominio non in allowlist: {url}. "
                    f"Consentiti: {', '.join(allow)}."
                )
        # URL anche dentro i messaggi di notify
        if tool_name in EXTERNAL_TEXT_TOOLS:
            for key in _TEXT_ARG_KEYS:
                for url in re.findall(r"https?://\S+", str(args.get(key, "") or "")):
                    if not _domain_allowed(url, allow):
                        return False, (
                            f"[EGRESS] il messaggio contiene un URL fuori "
                            f"allowlist: {url}."
                        )

    return True, ""
