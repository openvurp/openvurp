"""
openvurp Core — Model Router

Routing pragmatico tra orchestrator cloud e worker locali/più leggeri.
"""

from __future__ import annotations

from dataclasses import dataclass
import re

import config as cfg


@dataclass
class RoutedModelChoice:
    backend: str
    model: str
    thinking: str
    mode: str
    reason: str
    strategy: str


@dataclass
class ChatModelChoice:
    """Scelta economica per un turno della chat principale.

    Il classificatore e' deliberatamente locale e deterministico: spendere una
    chiamata LLM per decidere quale LLM chiamare annullerebbe buona parte del
    risparmio.
    """

    backend: str
    model: str
    tier: str
    reason: str
    strategy: str = "automatic_cost_router"


def _cfg(name: str, default=""):
    value = getattr(cfg, name, default)
    return value if value not in (None, "") else default


def _backend_looks_cloud(backend: str, model: str) -> bool:
    backend = (backend or "").strip().lower()
    model = (model or "").strip().lower()
    if backend in {"openai", "anthropic", "groq", "openai_compatible",
                   "codex", "claude_cli", "claude"}:
        return True
    return "cloud" in model


def _has_backend_credentials(backend: str) -> bool:
    backend = (backend or "").strip().lower()
    if backend == "ollama":
        return True
    if backend == "groq":
        return bool(_cfg("GROQ_API_KEY", _cfg("LLM_API_KEY", "")))
    if backend == "openai":
        return bool(_cfg("OPENAI_API_KEY", _cfg("LLM_API_KEY", "")))
    if backend == "anthropic":
        return bool(_cfg("ANTHROPIC_API_KEY", _cfg("LLM_API_KEY", "")))
    if backend in {"codex", "claude_cli", "claude"}:
        from core.cli_backends import claude_login_status, codex_login_status
        if backend == "codex":
            return codex_login_status(_cfg("CODEX_CLI", "codex"))[0]
        return claude_login_status(_cfg("CLAUDE_CLI", "claude"))[0]
    if backend == "openai_compatible":
        return bool(_cfg("OPENAI_COMPATIBLE_BASE_URL", _cfg("LLM_BASE_URL", "")))
    return False


def _ollama_base_url() -> str:
    return str(_cfg("LLM_BASE_URL", "http://localhost:11434") or "http://localhost:11434").rstrip("/")


def _parse_model_size(name: str) -> float:
    text = (name or "").lower()
    match = re.search(r"(\d+(?:\.\d+)?)b", text)
    if not match:
        return 999.0
    try:
        return float(match.group(1))
    except Exception:
        return 999.0


def discover_local_ollama_models(limit: int = 64) -> list[str]:
    try:
        import requests
        response = requests.get(f"{_ollama_base_url()}/api/tags", timeout=2.5)
        response.raise_for_status()
        data = response.json()
        models = []
        for item in data.get("models", [])[:limit]:
            name = str(item.get("name", "") or "").strip()
            if not name:
                continue
            lower = name.lower()
            if "cloud" in lower:
                continue
            models.append(name)
        return models
    except Exception:
        return []


def choose_small_local_ollama_model() -> str:
    models = discover_local_ollama_models()
    if not models:
        return ""
    preferred = sorted(
        models,
        key=lambda name: (
            _parse_model_size(name),
            0 if any(tag in name.lower() for tag in ("instruct", "chat")) else 1,
            len(name),
        ),
    )
    return preferred[0] if preferred else ""


def _task_is_executor_like(task: str, deliverable: str = "") -> bool:
    text = f"{task}\n{deliverable}".lower()
    keywords = (
        "run ", "esegui", "grep", "glob", "browser", "screenshot", "doctor",
        "list files", "cerca nel repo", "scan", "read file", "leggi file",
        "inspect", "check status", "log", "process", "tool", "command",
    )
    return any(keyword in text for keyword in keywords)


def _task_is_deep_reasoning(task: str, deliverable: str = "") -> bool:
    text = f"{task}\n{deliverable}".lower()
    keywords = (
        "architecture", "architett", "security review", "threat", "design",
        "compare", "tradeoff", "refactor plan", "migration", "debug root cause",
        "why", "perché", "postmortem", "strategy",
    )
    return any(keyword in text for keyword in keywords) or len(text) > 900


_CHAT_DEEP_PATTERNS = (
    "architettura", "architecture", "security review", "threat model",
    "migrazione", "migration", "root cause", "postmortem", "refactor completo",
    "analizza il progetto", "analizza tutto", "risolvi ogni cosa", "trade-off",
    "strategia completa", "piano completo", "sistema distribuito",
)


def _chat_needs_deep_model(prompt: str) -> bool:
    """Conservativo: il modello medio si usa solo quando porta valore reale."""
    text = " ".join(str(prompt or "").lower().split())
    if len(text) >= 1400:
        return True
    hits = sum(pattern in text for pattern in _CHAT_DEEP_PATTERNS)
    return hits >= 1


def route_chat_prompt(prompt: str) -> ChatModelChoice:
    """Sceglie il modello per ``Automatico economico`` senza chiamate extra.

    Di default usa soltanto il login Codex: Luna copre i turni normali, mentre
    Terra viene riservato ai prompt chiaramente complessi. Se Codex non e'
    autenticato, ripiega sul login Claude.ai; non seleziona API a consumo o
    Ollama implicitamente.
    """
    from core.cli_backends import claude_login_status, codex_login_status

    codex_binary = str(_cfg("CODEX_CLI", "codex") or "codex")
    codex_ok, _ = codex_login_status(codex_binary)
    if codex_ok:
        max_tier = str(_cfg("AUTO_ROUTER_MAX_TIER", "terra") or "terra").lower()
        deep = _chat_needs_deep_model(prompt) and max_tier in {"terra", "sol"}
        if deep:
            return ChatModelChoice(
                backend="codex",
                model=str(_cfg("AUTO_ROUTER_DEEP_MODEL", "gpt-5.6-terra")),
                tier="deep",
                reason="prompt complesso: uso Terra solo per questo turno",
            )
        return ChatModelChoice(
            backend="codex",
            model=str(_cfg("AUTO_ROUTER_FAST_MODEL", "gpt-5.6-luna")),
            tier="fast",
            reason="Luna e' sufficiente: priorita' a velocita' e consumo basso",
        )

    claude_binary = str(_cfg("CLAUDE_CLI", "claude") or "claude")
    claude_ok, _ = claude_login_status(claude_binary)
    if claude_ok:
        return ChatModelChoice(
            backend="claude_cli",
            model=str(_cfg("CLAUDE_CLI_MODEL", "sonnet")),
            tier="fallback",
            reason="Codex non disponibile: uso l'abbonamento Claude.ai",
            strategy="automatic_subscription_fallback",
        )

    return ChatModelChoice(
        backend=str(_cfg("LLM_BACKEND", "ollama")),
        model=str(_cfg("LLM_MODEL", "")),
        tier="fallback",
        reason="nessun CLI in abbonamento disponibile: uso il motore globale",
        strategy="automatic_global_fallback",
    )


def _configured_route(prefix: str) -> tuple[str, str]:
    backend = str(_cfg(f"{prefix}_BACKEND", "") or "").strip()
    model = str(_cfg(f"{prefix}_MODEL", "") or "").strip()
    return backend, model


def route_subagent(
    *,
    parent_backend: str,
    parent_model: str,
    task: str,
    deliverable: str = "",
    requested_backend: str = "",
    requested_model: str = "",
    requested_thinking: str = "",
    requested_mode: str = "",
) -> RoutedModelChoice:
    explicit_backend = str(requested_backend or "").strip()
    explicit_model = str(requested_model or "").strip()
    explicit_thinking = str(requested_thinking or "").strip()
    requested_mode = str(requested_mode or "").strip().lower()

    if explicit_backend or explicit_model:
        return RoutedModelChoice(
            backend=explicit_backend or parent_backend,
            model=explicit_model or parent_model,
            thinking=explicit_thinking or "off",
            mode=requested_mode or "text",
            reason="routing esplicito richiesto dal parent",
            strategy="explicit",
        )

    default_mode = str(_cfg("SUBAGENT_DEFAULT_MODE", "auto") or "auto").strip().lower()
    mode = requested_mode or default_mode or "auto"
    if mode == "auto":
        mode = "safe_executor" if _task_is_executor_like(task, deliverable) else "text"

    text_backend, text_model = _configured_route("SUBAGENT_TEXT")
    exec_backend, exec_model = _configured_route("SUBAGENT_EXECUTOR")
    analysis_backend, analysis_model = _configured_route("SUBAGENT_ANALYSIS")

    local_small = choose_small_local_ollama_model()
    parent_is_cloud = _backend_looks_cloud(parent_backend, parent_model)

    backend = parent_backend
    model = parent_model
    thinking = explicit_thinking or "off"
    reason = "fallback al parent"
    strategy = "inherit_parent"

    if mode in {"safe_executor", "inherit_executor"}:
        if exec_backend or exec_model:
            backend = exec_backend or parent_backend
            model = exec_model or parent_model
            reason = "executor route configurata"
            strategy = "configured_executor"
        elif local_small and _has_backend_credentials("ollama"):
            backend = "ollama"
            model = local_small
            reason = f"executor instradato su modello locale piccolo: {local_small}"
            strategy = "auto_local_executor"
        thinking = explicit_thinking or "off"
        return RoutedModelChoice(
            backend=backend,
            model=model,
            thinking=thinking,
            mode=mode,
            reason=reason,
            strategy=strategy,
        )

    if _task_is_deep_reasoning(task, deliverable):
        if analysis_backend or analysis_model:
            backend = analysis_backend or parent_backend
            model = analysis_model or parent_model
            reason = "analysis route configurata"
            strategy = "configured_analysis"
        else:
            backend = parent_backend
            model = parent_model
            reason = "task di reasoning profondo: resta sul parent"
            strategy = "deep_reasoning_parent"
        thinking = explicit_thinking or str(_cfg("SUBAGENT_DEFAULT_THINKING", "off") or "off")
        return RoutedModelChoice(
            backend=backend,
            model=model,
            thinking=thinking,
            mode="text",
            reason=reason,
            strategy=strategy,
        )

    if text_backend or text_model:
        backend = text_backend or parent_backend
        model = text_model or parent_model
        reason = "text route configurata"
        strategy = "configured_text"
    elif parent_is_cloud and local_small and _has_backend_credentials("ollama"):
        backend = "ollama"
        model = local_small
        reason = f"task bounded spostato su modello locale piccolo: {local_small}"
        strategy = "auto_local_text"
    else:
        backend = parent_backend
        model = parent_model
        reason = "nessun worker locale adatto: eredita il parent"
        strategy = "inherit_parent"

    thinking = explicit_thinking or str(_cfg("SUBAGENT_DEFAULT_THINKING", "off") or "off")
    return RoutedModelChoice(
        backend=backend,
        model=model,
        thinking=thinking,
        mode="text",
        reason=reason,
        strategy=strategy,
    )
