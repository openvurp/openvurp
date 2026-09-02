"""Catalogo pubblico dei provider configurabili dalla dashboard."""

from __future__ import annotations

import importlib.util

from core.cli_backends import claude_login_status, codex_login_status


_CATALOG_CACHE: dict = {"at": 0.0, "value": None}


def provider_catalog() -> list[dict]:
    """Catalogo dei motori, con una cache breve.

    Sapere se Codex e Claude sono autenticati significa lanciare due processi:
    farlo a ogni apertura della pagina si sente. Lo stato del login non cambia
    da un secondo all'altro.
    """
    import time

    if _CATALOG_CACHE["value"] is not None and time.time() - _CATALOG_CACHE["at"] < 60:
        return _CATALOG_CACHE["value"]
    catalog = _build_catalog()
    _CATALOG_CACHE.update(at=time.time(), value=catalog)
    return catalog


def _build_catalog() -> list[dict]:
    import config as cfg

    codex_ok, codex_auth = codex_login_status(
        getattr(cfg, "CODEX_CLI", "codex")
    )
    claude_ok, claude_auth = claude_login_status(
        getattr(cfg, "CLAUDE_CLI", "claude")
    )
    return [
        {
            "id": "auto", "label": "Automatico economico · Codex",
            "available": codex_ok or claude_ok,
            "default_model": "", "billing": "Luna; Terra solo se necessario",
            "auth": codex_auth if codex_ok else claude_auth,
        },
        {
            "id": "", "label": "Predefinito openvurp", "available": True,
            "default_model": getattr(cfg, "LLM_MODEL", ""), "billing": "configurazione globale",
        },
        {
            "id": "codex", "label": "Codex · abbonamento ChatGPT",
            "available": codex_ok, "auth": codex_auth,
            "default_model": getattr(cfg, "CODEX_MODEL", "gpt-5.6-luna"),
            "billing": "limiti inclusi ChatGPT",
        },
        {
            "id": "claude_cli", "label": "Claude · abbonamento Claude.ai",
            "available": claude_ok, "auth": claude_auth,
            "default_model": getattr(cfg, "CLAUDE_CLI_MODEL", "sonnet"),
            "billing": "limiti inclusi Claude.ai",
        },
        {
            "id": "ollama", "label": "Ollama · locale", "available": True,
            "default_model": getattr(cfg, "LLM_MODEL", ""), "billing": "locale",
        },
        {
            "id": "anthropic", "label": "Claude API · consumo separato",
            "available": bool(getattr(cfg, "ANTHROPIC_API_KEY", ""))
            and importlib.util.find_spec("anthropic") is not None,
            "default_model": getattr(cfg, "ANTHROPIC_MODEL", ""), "billing": "API",
        },
        {
            "id": "openai", "label": "OpenAI API · consumo separato",
            "available": bool(getattr(cfg, "OPENAI_API_KEY", ""))
            and importlib.util.find_spec("openai") is not None,
            "default_model": getattr(cfg, "OPENAI_MODEL", ""), "billing": "API",
        },
        {
            "id": "groq", "label": "Groq API", "available": bool(
                getattr(cfg, "GROQ_API_KEY", "")
            ) and importlib.util.find_spec("groq") is not None,
            "default_model": getattr(cfg, "GROQ_MODEL", ""), "billing": "API",
        },
        {
            "id": "openai_compatible", "label": "Server compatibile OpenAI",
            "available": bool(getattr(cfg, "OPENAI_COMPATIBLE_BASE_URL", "")),
            "default_model": getattr(cfg, "OPENAI_COMPATIBLE_MODEL", ""),
            "billing": "dipende dal server",
        },
    ]
