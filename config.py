"""
Configurazione openvurp.

I segreti non vivono in questo file: usa variabili d'ambiente o `.env`.
openvurp carica `.env` in modo minimale, senza dipendenze esterne, e non
sovrascrive variabili gia' presenti nell'ambiente.
"""

from __future__ import annotations

import os
from pathlib import Path


OPENVURP_DIR = Path(__file__).resolve().parent


def _load_dotenv(path: Path = OPENVURP_DIR / ".env") -> None:
    if not path.exists():
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = _env(name, "1" if default else "0").strip().lower()
    return raw in {"1", "true", "yes", "y", "on", "si", "sì"}


def _env_list(name: str, default: list[str] | None = None) -> list[str]:
    raw = _env(name, "")
    if not raw:
        return list(default or [])
    normalized = raw.replace(";", ",").replace(" ", ",")
    return [item.strip() for item in normalized.split(",") if item.strip()]


def _env_int_list(name: str, default: list[int] | None = None) -> list[int]:
    values: list[int] = []
    for item in _env_list(name, []):
        try:
            values.append(int(item))
        except ValueError:
            continue
    return values if values else list(default or [])


_load_dotenv()


# --- LLM backend ---
LLM_BACKEND = _env("LLM_BACKEND", "ollama")
LLM_MODEL = _env("LLM_MODEL", "qwen3-coder-next:cloud")
LLM_BASE_URL = _env("LLM_BASE_URL", "http://localhost:11434")
LLM_API_KEY = _env("LLM_API_KEY", "")
# Ragionamento dei modelli "thinking" (Ollama): false (default) = risposta
# direct, no reasoning monologue in chat; true = force thinking;
# auto = lascia decidere al modello (può far trapelare il ragionamento).
LLM_THINK = _env("LLM_THINK", "false")

GROQ_API_KEY = _env("GROQ_API_KEY", "")
OPENAI_API_KEY = _env("OPENAI_API_KEY", "")
OPENAI_BASE_URL = _env("OPENAI_BASE_URL", "")
ANTHROPIC_API_KEY = _env("ANTHROPIC_API_KEY", "")
OPENAI_COMPATIBLE_API_KEY = _env("OPENAI_COMPATIBLE_API_KEY", "")
OPENAI_COMPATIBLE_BASE_URL = _env("OPENAI_COMPATIBLE_BASE_URL", "")
OPENAI_COMPATIBLE_MODEL = _env("OPENAI_COMPATIBLE_MODEL", "")

# Subscription CLI providers: the runtime always strips API keys from the
# processo figlio, così Codex/Claude usano soltanto il login personale.
CODEX_CLI = _env("CODEX_CLI", "codex")
CODEX_MODEL = _env("CODEX_MODEL", "gpt-5.6-luna")
CODEX_SANDBOX = _env("CODEX_SANDBOX", "read-only")
# Quanto puo' restare in SILENZIO il provider prima di considerarlo morto.
# Non e' la durata massima della risposta: finche' arrivano parole, l'orologio
# riparte. Prima era la durata totale, e un'analisi lunga veniva troncata a
# meta' frase mentre stava ancora scrivendo.
CODEX_TIMEOUT_SECONDS = _env_int("CODEX_TIMEOUT_SECONDS", 300)
# Il tetto assoluto del turno, per il provider che parla e non conclude mai.
CODEX_MAX_TURN_SECONDS = _env_int("CODEX_MAX_TURN_SECONDS", 1800)
# Context sent to Codex. The old default (12,000) cut openvurp's system prompt
# in half: the identity, method and memory .md files never reached the model
# and the agent looked "forgetful". Codex handles far larger windows, so the
# default is now generous.
CODEX_CONTEXT_MAX_CHARS = _env_int("CODEX_CONTEXT_MAX_CHARS", 180000)
# The system prompt is instruction, not history: it must never be cut in the
# middle to make room for turns. This is its dedicated budget.
CLI_SYSTEM_PROMPT_MAX_CHARS = _env_int("CLI_SYSTEM_PROMPT_MAX_CHARS", 80000)
# Cap on a single result an openvurp tool returns to Codex. The runtime still
# keeps the full result in the audit; only a compact head/tail view enters the
# LLM context, to avoid token spikes.
CODEX_TOOL_RESULT_MAX_CHARS = _env_int("CODEX_TOOL_RESULT_MAX_CHARS", 8000)
CODEX_REQUIRE_CHATGPT_LOGIN = _env_bool("CODEX_REQUIRE_CHATGPT_LOGIN", True)
CLAUDE_CLI = _env("CLAUDE_CLI", "claude")
CLAUDE_CLI_MODEL = _env("CLAUDE_CLI_MODEL", "sonnet")
CLAUDE_CLI_TIMEOUT_SECONDS = _env_int("CLAUDE_CLI_TIMEOUT_SECONDS", 300)
CLAUDE_CLI_CONTEXT_MAX_CHARS = _env_int("CLAUDE_CLI_CONTEXT_MAX_CHARS", 180000)
CLAUDE_CLI_REQUIRE_SUBSCRIPTION_LOGIN = _env_bool(
    "CLAUDE_CLI_REQUIRE_SUBSCRIPTION_LOGIN", True
)
# Autonomy-loop rounds with an agentic CLI backend. The default used to be 1,
# on the assumption that Codex does everything in a single turn through dynamic
# tools. True while that channel works: as soon as a second pass was needed — a
# kernel review, a tool that arrived as text, a result to re-read — the turn
# ended immediately with "iteration limit reached" having concluded nothing.
# The loop still exits as soon as the
# risposta e' pronta, quindi questi giri si usano solo se servono davvero.
CLI_AGENT_MAX_ITERATIONS = _env_int("CLI_AGENT_MAX_ITERATIONS", 6)

# --- Router chat economico ---
# Routing is heuristic and local: it does not spend a second LLM call to
# classify the prompt. It never picks Ollama or metered APIs automatically.
AUTO_ROUTER_FAST_MODEL = _env("AUTO_ROUTER_FAST_MODEL", "gpt-5.6-luna")
AUTO_ROUTER_DEEP_MODEL = _env("AUTO_ROUTER_DEEP_MODEL", "gpt-5.6-terra")
AUTO_ROUTER_MAX_TIER = _env("AUTO_ROUTER_MAX_TIER", "terra")  # luna | terra

# --- Visione e audio ---
# Vision has a backend separate from chat: the Codex/Claude CLIs do not
# receive images, while the vision model can stay on Ollama (or cloud).
VISION_BACKEND = _env("VISION_BACKEND", "ollama")
VISION_MODEL = _env("VISION_MODEL", "minimax-m3:cloud")
AUDIO_MODEL = _env("AUDIO_MODEL", "base")
AUDIO_ENABLED = _env_bool("AUDIO_ENABLED", True)
AUDIO_TRANSCRIBE_ENABLED = _env_bool("AUDIO_TRANSCRIBE_ENABLED", AUDIO_ENABLED)

# --- Sicurezza runtime ---
# Tetto giornaliero di chiamate LLM (0 = illimitato): anti loop/runaway
DAILY_LLM_BUDGET = _env_int("DAILY_LLM_BUDGET", 250)
# Egress allowlist: domini consentiti per web_fetch/notify (vuoto = aperto,
# ma i segreti in uscita restano sempre bloccati). Es: "example.com, docs.python.org"
EGRESS_ALLOWLIST = _env("EGRESS_ALLOWLIST", "")
# Verifica integrità del codice core all'avvio
INTEGRITY_CHECK = _env_bool("INTEGRITY_CHECK", True)

# --- RBAC ---
# Role for unrecognised actors: guest | reader | user | power.
RBAC_DEFAULT_ROLE = _env("RBAC_DEFAULT_ROLE", "guest")

# --- Privacy router ---
# off = nessun routing; strict = sessioni private sempre su modello locale
# when the main backend is cloud; auto = local only for sensitive content
PRIVACY_MODE = _env("PRIVACY_MODE", "off")
PRIVACY_LOCAL_BACKEND = _env("PRIVACY_LOCAL_BACKEND", "ollama")
PRIVACY_LOCAL_MODEL = _env("PRIVACY_LOCAL_MODEL", "")

# --- Giudizio sul cervello (escalation) ---
# off = always the main model; auto = the questions that matter
# (decisioni, sicurezza, soldi, "secondo te") vanno al modello profondo.
ESCALATION_MODE = _env("ESCALATION_MODE", "off")
ESCALATION_DEEP_BACKEND = _env("ESCALATION_DEEP_BACKEND", "")
ESCALATION_DEEP_MODEL = _env("ESCALATION_DEEP_MODEL", "")

# --- Heartbeat autonomo ---
# Tool/LLM iteration cap per heartbeat cycle (autonomy budget)
HEARTBEAT_MAX_ITERATIONS = _env_int("HEARTBEAT_MAX_ITERATIONS", 8)

# --- Memoria semantica (vector + FTS5) ---
VECTOR_MEMORY_ENABLED = _env_bool("VECTOR_MEMORY_ENABLED", True)
# Sbiadimento: i ricordi mai richiamati per N giorni vengono archiviati
# in memory/.faded/ durante il ciclo notturno (i richiami li rinforzano).
MEMORY_FADE_ENABLED = _env_bool("MEMORY_FADE_ENABLED", True)
MEMORY_FADE_IDLE_DAYS = _env_int("MEMORY_FADE_IDLE_DAYS", 45)
EMBEDDING_PROVIDER = _env("EMBEDDING_PROVIDER", "ollama")
EMBEDDING_MODEL = _env("EMBEDDING_MODEL", "nomic-embed-text")
EMBEDDING_BASE_URL = _env("EMBEDDING_BASE_URL", "http://localhost:11434")

# --- Parametri ---
# Conservative values: the model can still use more iterations for complex
# tasks, but it does not burn dozens of calls on an ordinary chat.
MAX_ITERATIONS = _env_int("MAX_ITERATIONS", 20)
CHAT_MAX_ITERATIONS = _env_int("CHAT_MAX_ITERATIONS", 4)
TURN_TOKEN_BUDGET = _env_int("TURN_TOKEN_BUDGET", 48000)
MAX_TOKENS = _env_int("MAX_TOKENS", 4096)
TEMPERATURE = _env_float("TEMPERATURE", 0.7)
# Temperature used when the model has tools available:
# più bassa = tool calling più affidabile.
TOOL_TEMPERATURE = _env_float("TOOL_TEMPERATURE", 0.2)
# Token-by-token streaming in the agent loop (only UIs that support it).
STREAMING_ENABLED = _env_bool("STREAMING_ENABLED", True)
CONTEXT_MAX_TOKENS = _env_int("CONTEXT_MAX_TOKENS", 64000)
# Obiettivo economico prima del limite fisico del modello. Il runtime elimina
# output tool vecchi e turni remoti prima di superarlo.
CONTEXT_TARGET_TOKENS = _env_int("CONTEXT_TARGET_TOKENS", 24000)
COMPACT_THRESHOLD = _env_float("COMPACT_THRESHOLD", 0.60)
SESSION_HISTORY_MAX_MESSAGES = _env_int("SESSION_HISTORY_MAX_MESSAGES", 40)
SESSION_HISTORY_MAX_CHARS = _env_int("SESSION_HISTORY_MAX_CHARS", 60000)
MEMORY_RETRIEVAL_CHARS = _env_int("MEMORY_RETRIEVAL_CHARS", 3000)
CONTINUITY_PROMPT_CHARS = _env_int("CONTINUITY_PROMPT_CHARS", 2000)
SHELL = _env("OPENVURP_SHELL", "")  # vuoto = auto-detect coerente col runtime

# --- Subagent orchestration ---
SUBAGENT_MAX_DEPTH = _env_int("SUBAGENT_MAX_DEPTH", 3)
SUBAGENT_MAX_CONCURRENT = _env_int("SUBAGENT_MAX_CONCURRENT", 4)
SUBAGENT_TIMEOUT_SECONDS = _env_int("SUBAGENT_TIMEOUT_SECONDS", 180)
SUBAGENT_AUTO_ANNOUNCE = _env_bool("SUBAGENT_AUTO_ANNOUNCE", True)
SUBAGENT_RUNTIME = _env("SUBAGENT_RUNTIME", "process")  # process | inline
SUBAGENT_KILL_GRACE_SECONDS = _env_int("SUBAGENT_KILL_GRACE_SECONDS", 3)
SUBAGENT_DEFAULT_BACKEND = _env("SUBAGENT_DEFAULT_BACKEND", "")
SUBAGENT_DEFAULT_MODEL = _env("SUBAGENT_DEFAULT_MODEL", "")
SUBAGENT_DEFAULT_THINKING = _env("SUBAGENT_DEFAULT_THINKING", "off")
SUBAGENT_DEFAULT_MODE = _env("SUBAGENT_DEFAULT_MODE", "auto")


# --- Stanze multiplayer (agenti persistenti, separate dai subagent) ---
MULTIPLAYER_BACKEND = _env("MULTIPLAYER_BACKEND", "auto")
MULTIPLAYER_MODEL = _env("MULTIPLAYER_MODEL", "")
MULTIPLAYER_MAX_AGENTS = _env_int("MULTIPLAYER_MAX_AGENTS", 3)
# A discussion has no duration decided in advance: it runs while somebody has
# something to add, closes by itself when a round passes in silence, and you
# stop it from the page. This is only the safety cap, so that a room left alone
# does not run forever at your expense.
MULTIPLAYER_MAX_ROUNDS = _env_int("MULTIPLAYER_MAX_ROUNDS", 12)
MULTIPLAYER_MAX_TOKENS = _env_int("MULTIPLAYER_MAX_TOKENS", 900)
MULTIPLAYER_DAILY_CALL_BUDGET = _env_int("MULTIPLAYER_DAILY_CALL_BUDGET", 120)

# --- Router dei tool ---
# "off"    = tutti i tool registrati sono sempre esposti (comportamento storico,
#            the one the agent could do everything with).
# "wide"   = pack essenziali sempre attivi + espansione per keyword.
# "strict" = solo `core` + keyword (risparmia token, ma l'agente perde capacita').
TOOL_ROUTER_MODE = _env("TOOL_ROUTER_MODE", "off")

# --- Ricerca file (grep / find_files) ---
# On a slow mount (e.g. /mnt/c under WSL2) a full walk can take minutes and
# time the provider's turn out. Better a partial result that says so than a
# freeze: the tools state explicitly when they truncate.
SEARCH_TIME_BUDGET_SECONDS = _env_int("SEARCH_TIME_BUDGET_SECONDS", 20)
SEARCH_MAX_FILES_SCANNED = _env_int("SEARCH_MAX_FILES_SCANNED", 20000)
# Safety cap on EVERY tool: a handler that never returns must not
# poter congelare il turno.
TOOL_HARD_TIMEOUT_SECONDS = _env_int("TOOL_HARD_TIMEOUT_SECONDS", 120)
# How many times the same call may fail identically before the runtime stops
# executing it and forces the model to stop.
TOOL_MAX_IDENTICAL_FAILURES = _env_int("TOOL_MAX_IDENTICAL_FAILURES", 3)

# --- Avvio ---
# Choice menu when `openvurp` opens (backend/model, setup, diagnostics).
# Mettilo a false se preferisci che parta dritto in chat.
STARTUP_MENU = _env_bool("STARTUP_MENU", True)

# --- Sciame: specialisti creati dall'agente stesso ---
SWARM_ENABLED = _env_bool("SWARM_ENABLED", True)
SWARM_MAX_AGENTS = _env_int("SWARM_MAX_AGENTS", 6)
SWARM_MAX_ROUNDS = _env_int("SWARM_MAX_ROUNDS", 3)
SWARM_MAX_TOKENS = _env_int("SWARM_MAX_TOKENS", 1200)
SWARM_BACKEND = _env("SWARM_BACKEND", "")   # vuoto = stesso backend dell'agente
SWARM_MODEL = _env("SWARM_MODEL", "")
SWARM_DAILY_CALL_BUDGET = _env_int("SWARM_DAILY_CALL_BUDGET", 200)
SWARM_HISTORY_MESSAGES = _env_int("SWARM_HISTORY_MESSAGES", 12)
# Strumenti degli agenti della rubrica. Vuoto = l'insieme predefinito
# (leggere/cercare/guardare/ricordare/avvisare). Elenco separato da virgole per
# change it. Every call still goes through openvurp's approvals.
# Vuoto = gli agenti hanno gli stessi strumenti di openvurp (shell, file,
# processi, web...). Elenco separato da virgole per restringerli.
SWARM_TOOLS = _env("SWARM_TOOLS", "")
# By default an agent cannot rewrite openvurp itself (evolve_self,
# plugins, restart). Set true to lift that too.
SWARM_TOOLS_ALLOW_SELF_EDIT = _env_bool("SWARM_TOOLS_ALLOW_SELF_EDIT", False)
SWARM_TOOL_ROUNDS = _env_int("SWARM_TOOL_ROUNDS", 4)
# Chiacchiere: ogni tanto due agenti si scambiano due battute in "Tutti
# together", with nobody asking. They run without tools and with short
# replies. Every line is still a model call: from here you
# regola quanto spesso e quante volte al giorno.
SWARM_IDLE_CHAT = _env_bool("SWARM_IDLE_CHAT", True)
SWARM_IDLE_MIN_MINUTES = _env_int("SWARM_IDLE_MIN_MINUTES", 25)
SWARM_IDLE_MAX_MINUTES = _env_int("SWARM_IDLE_MAX_MINUTES", 90)
SWARM_IDLE_DAILY_MAX = _env_int("SWARM_IDLE_DAILY_MAX", 6)

# How long a sensitive action asked from the web waits before counting as
# denied. Silence does not authorise: once the time is up, it does not run.
WEB_APPROVAL_TIMEOUT = _env_int("WEB_APPROVAL_TIMEOUT", 180)
# Gli agenti dello sciame vivono nello stesso ChatStore della rubrica web
# (memory/chats/chats.db): one roster for CLI, tools, channels and dashboard.

# --- Sandbox shell/runtime ---
SANDBOX_MODE = _env("SANDBOX_MODE", "restricted")
SANDBOX_ALLOWED_PATHS = _env_list("SANDBOX_ALLOWED_PATHS", [])
SANDBOX_DOCKER_IMAGE = _env("SANDBOX_DOCKER_IMAGE", "python:3.12-slim")
SANDBOX_DOCKER_MEMORY = _env("SANDBOX_DOCKER_MEMORY", "512m")
SANDBOX_DOCKER_CPUS = _env("SANDBOX_DOCKER_CPUS", "1")
SANDBOX_DOCKER_NETWORK = _env("SANDBOX_DOCKER_NETWORK", "none")
SANDBOX_TIMEOUT = _env_int("SANDBOX_TIMEOUT", 120)

# --- Notifiche in uscita ---
# Telegram here is only an INTERCOM: openvurp writes to you when you are away
# from the computer (morning brief, "I'm done", a permission to approve). The
# inbound channel lives in channels/ and goes through the same conversation the
# web page uses.
TELEGRAM_TOKEN = _env("TELEGRAM_TOKEN", "")
# Where notifications land. TELEGRAM_ALLOWED_USERS is still read so existing
# .env files keep working: it was the list of who may write to the bot, and the
# first ID is also the right recipient.
TELEGRAM_ALLOWED_USERS = _env_int_list("TELEGRAM_ALLOWED_USERS", [])
TELEGRAM_CHAT_ID = _env("TELEGRAM_CHAT_ID", "")
# --- Canali in ENTRATA ---
# Which ones can be used to talk to the agents from outside. Empty = nobody:
# opening a door onto your own terminal cannot be the default.
# Es: CHANNELS_IN=telegram,discord
CHANNELS_IN = _env_list("CHANNELS_IN", [])
# Who may talk to it, per channel. Empty = nobody, for the same reason.
DISCORD_ALLOWED_USERS = _env_list("DISCORD_ALLOWED_USERS", [])
SLACK_ALLOWED_USERS = _env_list("SLACK_ALLOWED_USERS", [])
WHATSAPP_ALLOWED_USERS = _env_list("WHATSAPP_ALLOWED_USERS", [])

DISCORD_TOKEN = _env("DISCORD_TOKEN", "")
SLACK_BOT_TOKEN = _env("SLACK_BOT_TOKEN", "")
SLACK_APP_TOKEN = _env("SLACK_APP_TOKEN", "")
# WhatsApp goes through Baileys (an UNOFFICIAL WhatsApp Web client): QR from
# the Settings page, runs behind your router, needs Node.js. The risk is stated
# where you switch it on: Meta can ban the number — use a spare one.

# --- Voce (TTS / microfono) ---
VOICE_NAME = _env("VOICE_NAME", "it-IT-DiegoNeural")
VOICE_RATE = _env("VOICE_RATE", "+0%")
VOICE_ENABLED = _env_bool("VOICE_ENABLED", False)
VOICE_TOOLS_ENABLED = _env_bool("VOICE_TOOLS_ENABLED", VOICE_ENABLED)
MIC_ENABLED = _env_bool("MIC_ENABLED", VOICE_TOOLS_ENABLED)

# --- Cache LLM ---
CACHE_TTL = _env_int("CACHE_TTL", 300)

# --- Dashboard e gateway ---
DASHBOARD_ENABLED = _env_bool("DASHBOARD_ENABLED", False)
# Security: by default the dashboard listens ONLY on localhost (unreachable
# from the LAN). To expose it (e.g. Docker) set DASHBOARD_HOST=0.0.0.0 and
# ALWAYS a DASHBOARD_TOKEN; if exposed without one, the runtime generates a
# token and requires it.
DASHBOARD_HOST = _env("DASHBOARD_HOST", "127.0.0.1")
DASHBOARD_TOKEN = _env("DASHBOARD_TOKEN", "")
DASHBOARD_PORT = _env_int("DASHBOARD_PORT", 8420)
GATEWAY_ENABLED = _env_bool("GATEWAY_ENABLED", False)
GATEWAY_HOST = _env("GATEWAY_HOST", "127.0.0.1")
GATEWAY_PORT = _env_int("GATEWAY_PORT", 8421)

# --- Auto-update ---
# When on, the TUI periodically checks the git repo and applies updates fast-forward sicuri da sola (con smoke-test e rollback),
# then restarts. Meant for when the owner is away. Default: off.
AUTO_UPDATE = _env_bool("AUTO_UPDATE", False)
AUTO_UPDATE_INTERVAL = _env_int("AUTO_UPDATE_INTERVAL", 3600)  # secondi

# --- Browser ---
BROWSER_DEFAULT_MODE = _env("BROWSER_DEFAULT_MODE", "auto")
BROWSER_DEFAULT_ENGINE = _env("BROWSER_DEFAULT_ENGINE", "chromium")
BROWSER_DEFAULT_CHANNEL = _env("BROWSER_DEFAULT_CHANNEL", "")
BROWSER_DEFAULT_HEADLESS = _env_bool("BROWSER_DEFAULT_HEADLESS", True)
