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
# diretta, niente monologo di ragionamento in chat; true = forza il thinking;
# auto = lascia decidere al modello (può far trapelare il ragionamento).
LLM_THINK = _env("LLM_THINK", "false")

GROQ_API_KEY = _env("GROQ_API_KEY", "")
OPENAI_API_KEY = _env("OPENAI_API_KEY", "")
OPENAI_BASE_URL = _env("OPENAI_BASE_URL", "")
ANTHROPIC_API_KEY = _env("ANTHROPIC_API_KEY", "")
OPENAI_COMPATIBLE_API_KEY = _env("OPENAI_COMPATIBLE_API_KEY", "")
OPENAI_COMPATIBLE_BASE_URL = _env("OPENAI_COMPATIBLE_BASE_URL", "")

# --- Visione e audio ---
VISION_MODEL = _env("VISION_MODEL", "minimax-m3:cloud")
AUDIO_MODEL = _env("AUDIO_MODEL", "base")
AUDIO_ENABLED = _env_bool("AUDIO_ENABLED", True)
AUDIO_TRANSCRIBE_ENABLED = _env_bool("AUDIO_TRANSCRIBE_ENABLED", AUDIO_ENABLED)

# --- Sicurezza runtime ---
# Tetto giornaliero di chiamate LLM (0 = illimitato): anti loop/runaway
DAILY_LLM_BUDGET = _env_int("DAILY_LLM_BUDGET", 0)
# Egress allowlist: domini consentiti per web_fetch/notify (vuoto = aperto,
# ma i segreti in uscita restano sempre bloccati). Es: "example.com, docs.python.org"
EGRESS_ALLOWLIST = _env("EGRESS_ALLOWLIST", "")
# Verifica integrità del codice core all'avvio
INTEGRITY_CHECK = _env_bool("INTEGRITY_CHECK", True)

# --- RBAC ---
# Ruolo per attori non riconosciuti: guest | reader | user | power.
# Gli ID in TELEGRAM_ALLOWED_USERS sono sempre ADMIN (dispositivi dell'owner).
RBAC_DEFAULT_ROLE = _env("RBAC_DEFAULT_ROLE", "guest")

# --- Privacy router ---
# off = nessun routing; strict = sessioni private sempre su modello locale
# quando il backend principale è cloud; auto = locale solo su contenuti sensibili
PRIVACY_MODE = _env("PRIVACY_MODE", "off")
PRIVACY_LOCAL_BACKEND = _env("PRIVACY_LOCAL_BACKEND", "ollama")
PRIVACY_LOCAL_MODEL = _env("PRIVACY_LOCAL_MODEL", "")

# --- Giudizio sul cervello (escalation) ---
# off = sempre il modello principale; auto = le domande che contano
# (decisioni, sicurezza, soldi, "secondo te") vanno al modello profondo.
ESCALATION_MODE = _env("ESCALATION_MODE", "off")
ESCALATION_DEEP_BACKEND = _env("ESCALATION_DEEP_BACKEND", "")
ESCALATION_DEEP_MODEL = _env("ESCALATION_DEEP_MODEL", "")

# --- Heartbeat autonomo ---
# Tetto di iterazioni tool/LLM per ciclo heartbeat (budget autonomia)
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
MAX_ITERATIONS = _env_int("MAX_ITERATIONS", 50)
MAX_TOKENS = _env_int("MAX_TOKENS", 16384)
TEMPERATURE = _env_float("TEMPERATURE", 0.7)
# Temperatura usata quando il modello ha tool a disposizione:
# più bassa = tool calling più affidabile.
TOOL_TEMPERATURE = _env_float("TOOL_TEMPERATURE", 0.2)
# Streaming token-per-token nel loop agente (solo UI che lo supportano).
STREAMING_ENABLED = _env_bool("STREAMING_ENABLED", True)
CONTEXT_MAX_TOKENS = _env_int("CONTEXT_MAX_TOKENS", 128000)
COMPACT_THRESHOLD = _env_float("COMPACT_THRESHOLD", 0.75)
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

SUBAGENT_TEXT_BACKEND = _env("SUBAGENT_TEXT_BACKEND", "ollama")
SUBAGENT_TEXT_MODEL = _env("SUBAGENT_TEXT_MODEL", "nemotron-3-nano:30b-cloud")
SUBAGENT_EXECUTOR_BACKEND = _env("SUBAGENT_EXECUTOR_BACKEND", "ollama")
SUBAGENT_EXECUTOR_MODEL = _env("SUBAGENT_EXECUTOR_MODEL", "nemotron-3-nano:30b-cloud")
SUBAGENT_ANALYSIS_BACKEND = _env("SUBAGENT_ANALYSIS_BACKEND", "ollama")
SUBAGENT_ANALYSIS_MODEL = _env("SUBAGENT_ANALYSIS_MODEL", "nemotron-3-nano:30b-cloud")

# --- Sandbox shell/runtime ---
SANDBOX_MODE = _env("SANDBOX_MODE", "restricted")
SANDBOX_ALLOWED_PATHS = _env_list("SANDBOX_ALLOWED_PATHS", [])
SANDBOX_DOCKER_IMAGE = _env("SANDBOX_DOCKER_IMAGE", "python:3.12-slim")
SANDBOX_DOCKER_MEMORY = _env("SANDBOX_DOCKER_MEMORY", "512m")
SANDBOX_DOCKER_CPUS = _env("SANDBOX_DOCKER_CPUS", "1")
SANDBOX_DOCKER_NETWORK = _env("SANDBOX_DOCKER_NETWORK", "none")
SANDBOX_TIMEOUT = _env_int("SANDBOX_TIMEOUT", 120)

# --- Canali ---
TELEGRAM_TOKEN = _env("TELEGRAM_TOKEN", "")
TELEGRAM_ALLOWED_USERS = _env_int_list("TELEGRAM_ALLOWED_USERS", [])
# Comportamento nei GRUPPI Telegram:
#   mention = risponde solo se menzionato/in reply (default, zero costo extra)
#   natural = l'agente decide da solo quando intervenire, come una persona
#             (una decisione LLM leggera per messaggio, con cooldown)
#   all     = risponde a tutti i messaggi (rumoroso)
TELEGRAM_GROUP_MODE = _env("TELEGRAM_GROUP_MODE", "mention")
# Secondi minimi tra due interventi autonomi nello stesso gruppo (modo natural).
TELEGRAM_GROUP_COOLDOWN = _env_int("TELEGRAM_GROUP_COOLDOWN", 90)
# In 'natural' un modello PICCOLO/LOCALE fa il guardiano (legge il contesto del
# gruppo e decide se intervenire); quando dice sì, passa la palla al modello
# grande che risponde. Scegli tu quale modello locale usare qui (vuoto = usa il
# modello principale). Es: TELEGRAM_GROUP_DECIDER_MODEL=llama3.2:3b
TELEGRAM_GROUP_DECIDER_MODEL = _env("TELEGRAM_GROUP_DECIDER_MODEL", "")
TELEGRAM_GROUP_DECIDER_BACKEND = _env("TELEGRAM_GROUP_DECIDER_BACKEND", "ollama")
# Whitelist gruppi: se NON vuota, l'agente partecipa SOLO ai gruppi con questi
# chat_id (tutti gli altri vengono ignorati del tutto — niente risposta e niente
# memoria, anche se menzionato). Vuota = nessun limite. Il chat_id di un gruppo
# è negativo: quando ne arriva uno fuori lista, il CLI lo stampa una volta.
TELEGRAM_GROUP_WHITELIST = _env_list("TELEGRAM_GROUP_WHITELIST", [])
DISCORD_TOKEN = _env("DISCORD_TOKEN", "")
SLACK_BOT_TOKEN = _env("SLACK_BOT_TOKEN", "")
SLACK_APP_TOKEN = _env("SLACK_APP_TOKEN", "")

# --- Voce (TTS / microfono) ---
VOICE_NAME = _env("VOICE_NAME", "it-IT-DiegoNeural")
VOICE_RATE = _env("VOICE_RATE", "+0%")
VOICE_ENABLED = _env_bool("VOICE_ENABLED", False)
VOICE_TOOLS_ENABLED = _env_bool("VOICE_TOOLS_ENABLED", VOICE_ENABLED)
VOICE_BOOTSTRAP_ENABLED = _env_bool("VOICE_BOOTSTRAP_ENABLED", False)
MIC_ENABLED = _env_bool("MIC_ENABLED", VOICE_TOOLS_ENABLED)
TELEGRAM_VOICE_REPLY_ENABLED = _env_bool("TELEGRAM_VOICE_REPLY_ENABLED", False)

# --- Cache LLM ---
CACHE_TTL = _env_int("CACHE_TTL", 300)

# --- Dashboard e gateway ---
DASHBOARD_ENABLED = _env_bool("DASHBOARD_ENABLED", False)
# Sicurezza: di default la dashboard ascolta SOLO su localhost (non raggiungibile
# dalla LAN). Per esporla (es. Docker) imposta DASHBOARD_HOST=0.0.0.0 e SEMPRE un
# DASHBOARD_TOKEN; se esposta senza token, il runtime ne genera uno e lo richiede.
DASHBOARD_HOST = _env("DASHBOARD_HOST", "127.0.0.1")
DASHBOARD_TOKEN = _env("DASHBOARD_TOKEN", "")
DASHBOARD_PORT = _env_int("DASHBOARD_PORT", 8420)
GATEWAY_ENABLED = _env_bool("GATEWAY_ENABLED", False)
GATEWAY_HOST = _env("GATEWAY_HOST", "127.0.0.1")
GATEWAY_PORT = _env_int("GATEWAY_PORT", 8421)

# --- Auto-update ---
# Quando attivo, la TUI controlla periodicamente il repo git e applica gli
# aggiornamenti fast-forward sicuri da sola (con smoke-test e rollback),
# poi si riavvia. Pensato per quando l'owner non c'è. Default: off.
AUTO_UPDATE = _env_bool("AUTO_UPDATE", False)
AUTO_UPDATE_INTERVAL = _env_int("AUTO_UPDATE_INTERVAL", 3600)  # secondi

# --- Browser ---
BROWSER_DEFAULT_MODE = _env("BROWSER_DEFAULT_MODE", "auto")
BROWSER_DEFAULT_ENGINE = _env("BROWSER_DEFAULT_ENGINE", "chromium")
BROWSER_DEFAULT_CHANNEL = _env("BROWSER_DEFAULT_CHANNEL", "")
BROWSER_DEFAULT_HEADLESS = _env_bool("BROWSER_DEFAULT_HEADLESS", True)
