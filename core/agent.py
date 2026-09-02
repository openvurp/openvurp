"""
openvurp Core — Agent

Il cuore: loop LLM → tool → feedback, con reasoning, planning, safety, observability.
Function calling nativo per OpenAI/Anthropic/Groq, fallback regex per Ollama.
Sessioni separate per canale/sender.
"""

from __future__ import annotations

import re
import os
import json
import time
import uuid
from typing import Optional

from core.llm import LLMClient, LLMResponse, ToolCall, create_llm_client
from core.tools import ToolRegistry, ToolResult, ErrorType, Tool
from core.executor import Executor
from core.reasoner import Reasoner, ThinkingLevel
from core.planner import Planner, TaskPlan
from core.agent_state import AgentPhase, AgentStateMachine
from core.agent_kernel import AgentKernel, KernelPlan
from core.continuity import ContinuityPromptBuilder
from core.context import ContextManager, load_file, truncate_tool_result
from core.session import Session
from core.memory import MemoryManager
from core.learning import LearningLoop
from core.task_journal import TaskJournal
from core.safety import SafetyGuard, ActionRisk
from core.observer import Observer
from core.subagent import SubagentManager
from core.plugins import PluginManager
from core.personality import enhance_system_prompt, parse_response_directive, soften_reasoning, describe_venue
from core.doctor import build_doctor_report, fix_runtime_issues
from core.bootstrap import (
    BootstrapLoader,
    normalize_workspace_filename,
    resolve_session_type,
)
from core.environment import EnvironmentInspector, render_environment_prompt
from core.method import build_operating_method
from core.capabilities import inspect_runtime_capabilities, render_capability_prompt
from core.security.rbac import RBAC
from core.security.audit import AuditLog
from core.security.capability_lease import CapabilityLeaseManager
from core.runtime_gateway import RuntimeGateway
from core.session_routing import SessionRoute
from core.session_store import SessionStore
from core.tool_router import ToolRouter

# Import tool definitions
from tools.shell import SHELL_TOOL
from tools.file_ops import (READ_FILE_TOOL, WRITE_FILE_TOOL, EDIT_FILE_TOOL,
                            EDIT_LINES_TOOL, APPEND_FILE_TOOL)
from tools.search import GREP_TOOL, GLOB_TOOL
from tools.web import WEB_FETCH_TOOL, WEB_SEARCH_TOOL
from tools.process import (
    PROCESS_LIST_TOOL, PROCESS_SESSIONS_TOOL, PROCESS_START_TOOL,
    PROCESS_READ_TOOL, PROCESS_WRITE_TOOL, PROCESS_STOP_TOOL,
    PROCESS_KILL_TOOL,
)
from tools.notify import NOTIFY_TOOL, NOTIFY_VOICE_TOOL, NOTIFY_FILE_TOOL, NOTIFY_PHOTO_TOOL, NOTIFY_POLL_TOOL
from tools.scheduler import SCHEDULE_NOTIFY_TOOL, LIST_SCHEDULE_TOOL, CANCEL_SCHEDULE_TOOL
from tools.media import IMAGE_TOOL, AUDIO_TRANSCRIBE_TOOL, PDF_TOOL
from tools.desktop import DESKTOP_SCREENSHOT_TOOL
from tools.browser import BROWSER_TOOL
from tools.browser_setup import BROWSER_SETUP_TOOL
from tools.dreaming import MEMORY_CONSOLIDATE_TOOL
from tools.learning import (
    LEARNING_FEEDBACK_TOOL,
    LEARNING_REVIEW_TOOL,
    LEARNING_PROMOTE_TOOL,
    LEARNING_ROLLBACK_TOOL,
)
from tools.journal import (
    TASK_JOURNAL_TOOL,
    REFLECTION_NOTE_TOOL,
    OPEN_LOOP_TOOL,
)
from tools.plugins import SCAFFOLD_PLUGIN_TOOL

# Self-evolution tools
from tools.evolve import EVOLVE_SELF_TOOL, READ_SELF_TOOL

# Voice tools (opzionale)
try:
    from tools.voice_tools import SPEAK_TOOL, LISTEN_MIC_TOOL, LIST_VOICES_TOOL
    _HAS_VOICE = True
except ImportError:
    _HAS_VOICE = False

# Browser DevTools (opzionale)
try:
    from tools.browser_devtools import BROWSER_DEVTOOLS_TOOL
    _HAS_DEVTOOLS = True
except ImportError:
    _HAS_DEVTOOLS = False


OPENVURP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MEMORY_DIR = os.path.join(OPENVURP_DIR, "memory")
SKILLS_DIR = os.path.join(OPENVURP_DIR, "skills")
LOGS_DIR = os.path.join(OPENVURP_DIR, "logs")
PLUGINS_DIR = os.path.join(OPENVURP_DIR, "plugins")
CACHE_DIR = os.path.join(OPENVURP_DIR, "memory", "cache")


def _config_bool(name: str, default: bool = False) -> bool:
    try:
        import config as cfg
        value = getattr(cfg, name, default)
    except Exception:
        value = default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on", "si", "sì"}


class Agent:
    # Tool senza side-effect: un batch composto solo da questi
    # può essere eseguito in parallelo.
    PARALLEL_SAFE_TOOLS = {
        "read_file", "grep", "glob", "web_fetch", "web_search",
        "list_plugins", "process_list", "process_sessions",
    }

    # Tool permessi in plan mode: osservare sì, modificare no.
    PLAN_SAFE_TOOLS = {
        "read_file", "grep", "glob", "web_fetch", "web_search",
        "image_analyze", "pdf_read", "audio_transcribe",
        "process_list", "process_sessions", "process_read",
        "doctor", "list_plugins", "load_skill", "read_self",
        "learning_review", "task_journal", "agent_state",
        "desktop_screenshot", "browser",
    }

    # Modalità operative (stile Claude Code):
    #   safe = approvazioni normali; auto = pre-approva il non-critico;
    #   plan = solo osservazione, le modifiche diventano un piano.
    APPROVAL_MODES = ("safe", "auto", "plan")

    def __init__(self, ui):
        self.ui = ui

        # Temporary approval leases
        self.capability_leases = CapabilityLeaseManager(MEMORY_DIR)

        # Tool system
        self.tools = ToolRegistry()
        self._register_tools()
        self._builtin_tool_names = set(self.tools.names())
        self._plugin_tool_names: set[str] = set()
        self.tool_router = ToolRouter()
        self._active_tool_names = set(self.tool_router.selection.names)

        # Safety
        self.safety = SafetyGuard(openvurp_dir=OPENVURP_DIR)
        self.audit = AuditLog(os.path.join(MEMORY_DIR, "audit"))

        # Executor
        self.executor = Executor(
            registry=self.tools,
            safety=self.safety,
            audit_log=self.audit,
            lease_manager=self.capability_leases,
        )

        # Reasoning
        self.reasoner = Reasoner()
        self.planner = Planner()
        self.active_plan: Optional[TaskPlan] = None
        self.kernel = AgentKernel()
        self._active_kernel_plan: KernelPlan | None = None

        # Context
        try:
            import config as _cfg
            _ctx_max = getattr(_cfg, "CONTEXT_MAX_TOKENS", 128000)
            _compact = getattr(_cfg, "COMPACT_THRESHOLD", 0.75)
        except Exception:
            _ctx_max, _compact = 128000, 0.75
        self.context_mgr = ContextManager(
            openvurp_dir=OPENVURP_DIR,
            memory_dir=MEMORY_DIR,
            skills_dir=SKILLS_DIR,
            max_tokens=_ctx_max,
            compact_threshold=_compact,
        )

        # Memory
        self.memory = MemoryManager(MEMORY_DIR)
        self.learning = LearningLoop(MEMORY_DIR)
        self.journal = TaskJournal(MEMORY_DIR)
        self.agent_state = AgentStateMachine(MEMORY_DIR)
        self.continuity = ContinuityPromptBuilder(self.agent_state, self.journal)
        self._default_agent_state = self.agent_state
        self._route_agent_states: dict[str, AgentStateMachine] = {}
        self.rbac = RBAC(MEMORY_DIR)
        self.gateway = RuntimeGateway(OPENVURP_DIR)
        self.session_store = SessionStore(MEMORY_DIR)
        self._active_actor_id = "cli_owner"
        self._active_channel = "cli"
        self._active_route = SessionRoute.build()
        self._active_addressed = True  # messaggio interpellato? (False = gruppo non indirizzato)
        self._active_chat_type = ""    # "private" | "group" | "supergroup" | "" (es. CLI)
        # Cleanup automatico all'avvio
        try:
            self.memory.cleanup()
        except Exception:
            pass

        # Atto di nascita: dopo un reset il file sparisce e la prima
        # esecuzione successiva registra una nuova nascita.
        try:
            from core.growth import ensure_birth
            ensure_birth(MEMORY_DIR)
        except Exception:
            pass

        # Modalità operativa persistita (memory/runtime sopravvive ai reset memoria)
        self.approval_mode = self._load_approval_mode()

        # Budget giornaliero di chiamate LLM (anti loop/runaway)
        try:
            from core.budget import DailyBudget
            import config as _bcfg
            self.budget = DailyBudget(
                MEMORY_DIR, max_calls=getattr(_bcfg, "DAILY_LLM_BUDGET", 0)
            )
        except Exception:
            self.budget = None

        # Integrity check: rileva modifiche al codice core fuori da evolve_self
        self._check_integrity()

        # Session principale (CLI) + sessioni per canale
        sessions_dir = os.path.join(MEMORY_DIR, "sessions")
        self.session = Session(session_dir=sessions_dir)
        self._channel_sessions: dict[str, list[dict]] = {}
        self._route_runtime_sessions: dict[str, Session] = {}
        self._active_runtime_session = self.session
        self._restart_context: str = ""  # Impostato da main.py se è un riavvio

        # Observer
        self.observer = Observer(log_dir=LOGS_DIR)
        self.executor.observer = self.observer

        # LLM
        self.llm = create_llm_client()
        # LLM attivo per il turno corrente (può divergere da self.llm
        # quando il privacy router instrada su un modello locale)
        self._active_llm = self.llm
        self._privacy_llm = None  # lazy
        self._privacy_warned = False
        self._deep_llm = None  # lazy: modello profondo per escalation
        self._override_llms: dict[tuple[str, str], LLMClient] = {}
        self._last_llm_route = {
            "backend": self.llm.backend,
            "model": self.llm.model,
            "strategy": "default",
            "reason": "",
        }

        # Cache LLM
        try:
            from core.cache import LLMCache
            import config as cfg
            cache_ttl = getattr(cfg, 'CACHE_TTL', 300)
            if cache_ttl > 0:
                self.llm._cache = LLMCache(CACHE_DIR, ttl_seconds=cache_ttl)
        except Exception:
            pass

        # Subagent
        self.subagent_mgr = SubagentManager(self)

        # Sciame: specialisti persistenti che l'agente convoca da solo quando
        # ha un dubbio. Se non parte, l'agente resta pienamente funzionante.
        self.swarm = None
        try:
            import config as cfg
            swarm_on = bool(getattr(cfg, "SWARM_ENABLED", True))
        except Exception:
            swarm_on = True
        if swarm_on:
            try:
                from core.swarm import Swarm
                # Stesso archivio della rubrica in dashboard: un solo elenco
                # di agenti per CLI, tool, Telegram e web.
                self.swarm = Swarm(self, memory_dir=MEMORY_DIR)
            except Exception:
                self.swarm = None

        # Plugin system
        self.plugin_mgr = PluginManager(PLUGINS_DIR)
        try:
            self._load_plugin_tools()
        except Exception:
            pass

        # Bootstrap loader — rilegge file workspace da disco ogni turno
        self.bootstrap = BootstrapLoader(OPENVURP_DIR)
        self.environment = EnvironmentInspector(OPENVURP_DIR)

        # Anima: identità strutturata e versionata. Quando ha tratti attivi
        # sostituisce SOUL.md/IDENTITY.md/USER.md nel prompt.
        try:
            from core.anima import Anima
            self.anima = Anima(OPENVURP_DIR)
        except Exception:
            self.anima = None

        # Patti: accordi owner-agente applicati dal runtime
        try:
            from core.pacts import Pacts
            self.pacts = Pacts(MEMORY_DIR)
        except Exception:
            self.pacts = None

        # Curiosità: domande aperte studiate nei cicli morti
        try:
            from core.curiosity import Curiosity
            self.curiosity = Curiosity(MEMORY_DIR)
        except Exception:
            self.curiosity = None

        # Progetti: obiettivi a lungo termine, un prossimo passo alla volta
        try:
            from core.projects import Projects
            self.projects = Projects(MEMORY_DIR)
        except Exception:
            self.projects = None

        # Fucina: auto-estensione delle capacità (plugin forgiati e testati)
        try:
            from core.forge import Forge
            self.forge = Forge(MEMORY_DIR, OPENVURP_DIR)
        except Exception:
            self.forge = None

        # Sensi: percezione continua del mondo dell'owner
        try:
            from core.senses import Senses
            self.senses = Senses(MEMORY_DIR)
        except Exception:
            self.senses = None

        # Presenza: dove si trova l'owner adesso (per l'iniziativa)
        try:
            from core.presence import Presence
            self.presence = Presence(MEMORY_DIR)
        except Exception:
            self.presence = None

        # Legame: fili da richiedere, silenzio, ritmo dei messaggi spontanei
        try:
            from core.bonds import Bonds
            self.bonds = Bonds(MEMORY_DIR)
        except Exception:
            self.bonds = None

        # vurpub: il bancone condiviso (registry privato di skill/soluzioni).
        # Registra i tool QUI, dopo che self.tools esiste e self.vurpub è pronto
        # (_register_tools gira prima di questo punto).
        try:
            from core.vurpub import Vurpub
            self.vurpub = Vurpub(OPENVURP_DIR)
            self._register_vurpub_tools()
        except Exception:
            self.vurpub = None

        # Sorgente del turno corrente (cli/telegram/heartbeat/...): serve ai
        # tool che si comportano diversamente nei cicli autonomi.
        self._current_tool_source = "cli"

        # MCP
        self._mcp_client = None
        self._init_mcp()

        # Conversation state (sessione CLI default)
        self.messages: list[dict] = []
        self._build_system_prompt()

    def _sync_route_snapshot(self, route: SessionRoute, messages: list[dict],
                             runtime_session: Session, state: str = "idle") -> None:
        try:
            self.session_store.upsert(route, runtime_session, messages, state=state)
        except Exception:
            pass

    def restore_conversation(self):
        """Ripristina la conversazione dall'ultima sessione (dopo riavvio)."""
        sessions_dir = os.path.join(MEMORY_DIR, "sessions")
        prev_messages = Session.load_last_conversation(sessions_dir)
        if not prev_messages:
            return

        # Mantieni il system prompt attuale come primo messaggio
        system_msg = None
        if self.messages and self.messages[0]["role"] == "system":
            system_msg = self.messages[0]

        self.messages = []
        if system_msg:
            self.messages.append(system_msg)

        # Ripristina i messaggi della sessione precedente
        self.messages.extend(prev_messages)

    def _build_route(self, source: str = "cli", sender: str = "user",
                     actor_id: str = "cli_owner", chat_id: str = "",
                     thread_id: str = "", session_key: str = "",
                     parent_session_key: str = "") -> SessionRoute:
        return SessionRoute.build(
            source=source,
            sender=sender,
            actor_id=actor_id,
            chat_id=chat_id,
            thread_id=thread_id,
            session_key=session_key,
            parent_session_key=parent_session_key,
        )

    def _get_runtime_session(self, route: SessionRoute) -> Session:
        if route.source == "cli" and route.session_key == "cli:main":
            return self.session
        if route.session_key not in self._route_runtime_sessions:
            sessions_dir = os.path.join(MEMORY_DIR, "sessions")
            self._route_runtime_sessions[route.session_key] = Session(session_dir=sessions_dir)
        return self._route_runtime_sessions[route.session_key]

    def _agent_state_for_route(self, route: SessionRoute) -> AgentStateMachine:
        """Isola goal e stato operativo tra chat/canali differenti."""
        if route.source == "cli" and route.session_key == "cli:main":
            return self._default_agent_state
        state = self._route_agent_states.get(route.session_key)
        if state is None:
            state = AgentStateMachine(MEMORY_DIR, scope_key=route.session_key)
            self._route_agent_states[route.session_key] = state
        return state

    def _check_integrity(self):
        """Verifica integrità del codice core all'avvio.

        Prima esecuzione: crea il baseline in silenzio. Esecuzioni successive:
        avvisa (non blocca) se file core sono cambiati. Disattivabile con
        INTEGRITY_CHECK=false.
        """
        try:
            import config as cfg
            if not _config_bool("INTEGRITY_CHECK", True):
                return
            from core.security.integrity import IntegrityChecker
            checker = IntegrityChecker(OPENVURP_DIR)
            baseline_path = os.path.join(OPENVURP_DIR, IntegrityChecker.BASELINE_FILE)
            if not os.path.exists(baseline_path):
                checker.create_baseline()
                return
            report = checker.verify()
            if not report.valid and (report.modified or report.missing):
                changed = ", ".join((report.modified + report.missing)[:6])
                self.ui.status(
                    f"[integrity: core code changed since last baseline "
                    f"({changed}). If it was you or the agent, refresh with "
                    f"/integrity refresh; otherwise investigate.]"
                )
        except Exception:
            pass

    def refresh_integrity_baseline(self) -> str:
        try:
            from core.security.integrity import IntegrityChecker
            n = IntegrityChecker(OPENVURP_DIR).create_baseline()
            return f"Integrity baseline updated: {n} files."
        except Exception as e:
            return f"Cannot update the baseline: {e}"

    def _approval_mode_path(self) -> str:
        return os.path.join(MEMORY_DIR, "runtime", "approval_mode.json")

    def _load_approval_mode(self) -> str:
        try:
            with open(self._approval_mode_path(), "r", encoding="utf-8") as f:
                mode = json.load(f).get("mode", "safe")
            return mode if mode in self.APPROVAL_MODES else "safe"
        except Exception:
            return "safe"

    def set_approval_mode(self, mode: str) -> str:
        mode = (mode or "").strip().lower()
        if mode not in self.APPROVAL_MODES:
            return f"Modalità sconosciuta: {mode}. Disponibili: {', '.join(self.APPROVAL_MODES)}"
        self.approval_mode = mode
        try:
            path = self._approval_mode_path()
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"mode": mode}, f)
        except Exception:
            pass
        return ""

    def _select_llm(self, session_type: str, user_input: str):
        """Privacy router: sceglie il client LLM per il turno.

        Con PRIVACY_MODE=strict le sessioni private girano su un modello
        locale quando il backend principale è cloud. Garanzia del runtime,
        non del prompt. Returns (client, reason) — reason vuota = default.
        """
        try:
            import config as cfg
            from core.privacy import decide, resolve_local_model

            mode = str(getattr(cfg, "PRIVACY_MODE", "off") or "off")
            decision = decide(
                mode=mode,
                session_type=session_type,
                user_input=user_input,
                main_backend=self.llm.backend,
                main_model=self.llm.model,
            )
            if not decision.route_local:
                return self._maybe_escalate(session_type, user_input)

            if self._privacy_llm is None:
                backend, model = resolve_local_model()
                if not backend or not model:
                    if not self._privacy_warned:
                        self._privacy_warned = True
                        self.ui.status(
                            "[privacy: nessun modello locale disponibile — "
                            "resto sul backend principale]"
                        )
                    return self.llm, ""
                self._privacy_llm = create_llm_client(backend=backend, model=model)

            return self._privacy_llm, decision.reason
        except Exception:
            return self.llm, ""

    def _maybe_escalate(self, session_type: str, user_input: str):
        """Giudizio sul proprio cervello: le domande che contano vanno al
        modello profondo (ESCALATION_MODE=auto + ESCALATION_DEEP_*).

        La privacy ha già deciso che questo turno può andare sul cloud;
        qui si decide solo QUANTO pensare. Returns (client, reason).
        """
        try:
            from core.escalation import (
                decide_effort, escalation_mode, resolve_deep_model,
            )
            if escalation_mode() != "auto" or session_type == "heartbeat":
                return self.llm, ""
            decision = decide_effort(user_input)
            if not decision.route_deep:
                return self.llm, ""
            if self.budget is not None and self.budget.over_budget():
                return self.llm, ""
            if self._deep_llm is None:
                backend, model = resolve_deep_model()
                if not backend or not model:
                    return self.llm, ""
                if backend == self.llm.backend and model == self.llm.model:
                    return self.llm, ""
                self._deep_llm = create_llm_client(backend=backend, model=model)
            self.ui.status(
                f"[questa merita il modello profondo: {decision.reason}]"
            )
            return self._deep_llm, f"escalation: {decision.reason}"
        except Exception:
            return self.llm, ""

    def _configured_llm(self, backend: str, model: str) -> LLMClient:
        """Client riusabile scelto esplicitamente da una chat/stanza."""
        requested_backend = (backend or self.llm.backend).strip().lower()
        requested_model = (model or "").strip()
        key = (requested_backend, requested_model)
        client = self._override_llms.get(key)
        if client is None:
            client = create_llm_client(
                backend=requested_backend, model=requested_model,
            )
            self._override_llms[key] = client
        return client

    def _automatic_llm(self, user_input: str, session_type: str):
        """Router economico a costo zero per la chat principale.

        La scelta avviene con euristiche locali. Il privacy router viene
        applicato dopo la scelta, usando il backend candidato reale.
        """
        import config as cfg
        from core.model_router import route_chat_prompt

        choice = route_chat_prompt(user_input)
        backend, model = choice.backend, choice.model
        reason = choice.reason
        strategy = choice.strategy

        try:
            from core.privacy import decide, resolve_local_model

            privacy = decide(
                mode=str(getattr(cfg, "PRIVACY_MODE", "off") or "off"),
                session_type=session_type,
                user_input=user_input,
                main_backend=backend,
                main_model=model,
            )
            if privacy.route_local:
                local_backend, local_model = resolve_local_model()
                if local_backend and local_model:
                    backend, model = local_backend, local_model
                    reason = f"privacy: {privacy.reason}"
                    strategy = "automatic_privacy_local"
                else:
                    self.ui.status(
                        "[privacy: nessun modello locale disponibile; "
                        "mantengo il motore automatico]"
                    )
        except Exception:
            pass

        client = self._configured_llm(backend, model)
        return client, {
            "backend": client.backend,
            "model": client.model,
            "strategy": strategy,
            "reason": reason,
        }

    def _register_tools(self):
        """Registra tutti i tool disponibili."""
        base_tools = [
            SHELL_TOOL, READ_FILE_TOOL, WRITE_FILE_TOOL, EDIT_FILE_TOOL,
            EDIT_LINES_TOOL, APPEND_FILE_TOOL,
            GREP_TOOL, GLOB_TOOL, WEB_FETCH_TOOL, WEB_SEARCH_TOOL,
            PROCESS_LIST_TOOL, PROCESS_SESSIONS_TOOL,
            PROCESS_START_TOOL, PROCESS_READ_TOOL,
            PROCESS_WRITE_TOOL, PROCESS_STOP_TOOL,
            PROCESS_KILL_TOOL,
            IMAGE_TOOL, PDF_TOOL,
            DESKTOP_SCREENSHOT_TOOL, BROWSER_TOOL, BROWSER_SETUP_TOOL,
            MEMORY_CONSOLIDATE_TOOL, LEARNING_FEEDBACK_TOOL,
            LEARNING_REVIEW_TOOL, LEARNING_PROMOTE_TOOL,
            LEARNING_ROLLBACK_TOOL,
            TASK_JOURNAL_TOOL, REFLECTION_NOTE_TOOL, OPEN_LOOP_TOOL,
        ]
        if _config_bool("AUDIO_ENABLED", True) and _config_bool("AUDIO_TRANSCRIBE_ENABLED", True):
            base_tools.insert(base_tools.index(PDF_TOOL), AUDIO_TRANSCRIBE_TOOL)

        for tool in base_tools:
            self.tools.register(tool)
        self.tools.register(SCAFFOLD_PLUGIN_TOOL)
        self.tools.register(Tool(
            name="pact",
            description=(
                "Gestisce i patti: accordi espliciti con l'owner che il runtime "
                "fa rispettare anche quando il modello sbaglia. Usalo SOLO dopo "
                "che l'owner ha concordato il patto in conversazione. Tipi: "
                "protected_path (mai scrivere/toccare un path), confirm_external "
                "(mai azioni esterne senza conferma esplicita)."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "description": "add | retire | list"},
                    "pact_type": {"type": "string", "description": "protected_path | confirm_external (per add)."},
                    "description": {"type": "string", "description": "Il patto, come concordato con l'owner."},
                    "pattern": {"type": "string", "description": "Per protected_path: il path da proteggere."},
                    "reason": {"type": "string", "description": "Perché è stato stretto o sciolto."},
                    "pact_id": {"type": "string", "description": "ID del patto (per retire)."},
                },
                "required": ["action"],
            },
            handler=self._pact_handler,
        ))
        self.tools.register(Tool(
            name="curiosity",
            description=(
                "La tua lista di domande aperte: cose che hai notato di non "
                "sapere sul mondo dell'owner e che varrebbe la pena studiare. "
                "Aggiungi domande quando emergono in conversazione (add); nei "
                "cicli autonomi studiane una e chiudila (answer) salvando cosa "
                "hai imparato. Azioni: add, answer, drop, list."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "description": "add | answer | drop | list"},
                    "question": {"type": "string", "description": "La domanda (per add)."},
                    "why": {"type": "string", "description": "Perché vale la pena saperlo (per add)."},
                    "question_id": {"type": "string", "description": "ID della domanda (per answer/drop)."},
                    "summary": {"type": "string", "description": "Cosa hai imparato (per answer)."},
                },
                "required": ["action"],
            },
            handler=self._curiosity_handler,
        ))
        self.tools.register(Tool(
            name="project",
            description=(
                "I tuoi progetti a lungo termine: obiettivi concordati con "
                "l'owner che durano settimane e sopravvivono ai riavvii. Ogni "
                "progetto ha UN prossimo passo concreto. Quando l'owner concorda "
                "un obiettivo grande, registralo (create); quando lavori e fai "
                "progressi, annotali (note) e aggiorna il prossimo passo; nei "
                "cicli autonomi avanza il prossimo passo di un progetto fermo. "
                "Azioni: create, note, set_next, milestone_add, milestone_done, "
                "pause, resume, complete, drop, list."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "description": "create | note | set_next | milestone_add | milestone_done | pause | resume | complete | drop | list"},
                    "title": {"type": "string", "description": "Titolo del progetto (per create)."},
                    "goal": {"type": "string", "description": "Per create: come si capisce che è finito (criterio concreto)."},
                    "why": {"type": "string", "description": "Perché conta per l'owner (per create)."},
                    "next_step": {"type": "string", "description": "Il prossimo passo concreto (per create/note/set_next)."},
                    "target_date": {"type": "string", "description": "Scadenza ISO opzionale, es. 2026-07-15 (per create)."},
                    "project_id": {"type": "string", "description": "ID del progetto (per tutte le azioni tranne create/list)."},
                    "note": {"type": "string", "description": "Cosa è stato fatto (per note) o esito (per complete)."},
                    "milestone": {"type": "string", "description": "Titolo milestone (milestone_add) o indice/titolo (milestone_done)."},
                    "reason": {"type": "string", "description": "Motivo (per pause/drop)."},
                },
                "required": ["action"],
            },
            handler=self._project_handler,
        ))
        self.tools.register(Tool(
            name="forge",
            description=(
                "La fucina: quando ti manca una capacità (un tool che non hai), "
                "non arrenderti — forgiala. Ciclo: propose (dichiara la lacuna) → "
                "scrivi il plugin con scaffold_plugin/write_file, includendo una "
                "funzione selftest() che PROVA che funziona → draft → test (il "
                "selftest gira in un processo isolato) → adopt (solo col via "
                "libera dell'owner, mai nei cicli autonomi). Le capacità adottate "
                "diventano tool veri. Azioni: propose, draft, test, adopt, "
                "reject, retire, list."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "description": "propose | draft | test | adopt | reject | retire | list"},
                    "plugin_id": {"type": "string", "description": "Identificatore del plugin, es. rss_reader (per propose)."},
                    "need": {"type": "string", "description": "La lacuna: cosa non riesci a fare oggi e in quale situazione ti è mancato (per propose)."},
                    "why": {"type": "string", "description": "Perché vale la pena (per propose)."},
                    "forge_id": {"type": "string", "description": "ID della voce di fucina (per draft/test/adopt/reject/retire)."},
                    "reason": {"type": "string", "description": "Motivo (per reject/retire)."},
                },
                "required": ["action"],
            },
            handler=self._forge_handler,
        ))
        self.tools.register(Tool(
            name="sense",
            description=(
                "I tuoi sensi: sorgenti che osservi da solo tra un heartbeat e "
                "l'altro (cartelle, file, pagine web, feed RSS). Quando l'owner "
                "dice 'tieni d'occhio X' o noti una sorgente che vale la pena "
                "seguire per i suoi progetti, apri un senso (add). Le novità ti "
                "arrivano nei cicli autonomi: collegale ai progetti/curiosità e, "
                "se gli interessano davvero, scrivigli tu. "
                "Azioni: add, remove, list."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "description": "add | remove | list"},
                    "kind": {"type": "string", "description": "folder | file | url | rss (per add)."},
                    "target": {"type": "string", "description": "Path della cartella/file o URL (per add)."},
                    "label": {"type": "string", "description": "Etichetta parlante, es. 'cartella Tesi' (per add)."},
                    "why": {"type": "string", "description": "Cosa interessa all'owner di questa sorgente (per add)."},
                    "sense_id": {"type": "string", "description": "ID del senso (per remove)."},
                },
                "required": ["action"],
            },
            handler=self._sense_handler,
        ))
        # Only offered when there is actually a second model to ask. Before,
        # the tool was always in the box and always failed with "configure
        # ESCALATION_DEEP_BACKEND": the agent spent a turn discovering that a
        # tool it had been handed does not work. A tool that cannot work
        # should not be on the list.
        from core.escalation import resolve_deep_model
        if all(resolve_deep_model()):
            self._register_second_opinion()
        self._register_remaining_tools()

    def _register_second_opinion(self) -> None:
        self.tools.register(Tool(
            name="second_opinion",
            description=(
                "Asks an independent opinion of a different (deeper) model "
                "BEFORE answering on things that matter: important decisions, "
                "architecture, security, money. Use it when the cost of being "
                "wrong is high, or when the owner deserves more than one "
                "perspective. Always report it when the second opinion "
                "disagrees with yours."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "question": {"type": "string", "description": "The question, put neutrally (without hinting at the answer)."},
                    "context": {"type": "string", "description": "The context needed for the opinion to be informed."},
                },
                "required": ["question"],
            },
            handler=self._second_opinion_handler,
        ))

    def _register_remaining_tools(self) -> None:
        self.tools.register(Tool(
            name="follow_up",
            description=(
                "I fili del legame con l'owner: quando racconta qualcosa che ha "
                "un dopo ('domani ho il colloquio', 'stasera la partita', "
                "'lunedì l'esame'), lega un filo (add) con QUANDO ha senso "
                "richiedere. Al momento giusto, nei cicli autonomi, scrivigli TU "
                "per chiedere com'è andata (poi action=asked); quando ti "
                "racconta l'esito, chiudi il filo (close) salvandolo. È quello "
                "che farebbe un amico vero. Azioni: add, asked, close, list."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "description": "add | asked | close | list"},
                    "what": {"type": "string", "description": "Cosa succede nella vita dell'owner (per add)."},
                    "due": {"type": "string", "description": "Quando chiedere, ISO: es. 2026-06-13T18:00 (per add)."},
                    "why": {"type": "string", "description": "Perché conta per lui (per add)."},
                    "thread_id": {"type": "string", "description": "ID del filo (per asked/close)."},
                    "outcome": {"type": "string", "description": "Com'è andata (per close)."},
                },
                "required": ["action"],
            },
            handler=self._follow_up_handler,
        ))
        self.tools.register(Tool(
            name="anima_update",
            description=(
                "Fa evolvere la tua Anima: l'identità strutturata che sostituisce "
                "SOUL.md/IDENTITY.md/USER.md quando attiva. Ogni tratto ha sezione "
                "(identity, voice, boundaries, owner, method), origine, versione e "
                "storia. Le mutazioni sono verificate (niente segreti/duplicati) e "
                "hanno un budget giornaliero anti-drift. Usa azioni: add, revise, "
                "retire, restore. Informa l'owner quando cambi chi sei."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "description": "add | revise | retire | restore",
                    },
                    "section": {
                        "type": "string",
                        "description": "Per add: identity, voice, boundaries, owner, method.",
                    },
                    "text": {
                        "type": "string",
                        "description": "Testo del tratto (per add/revise). Una o due frasi, autosufficienti.",
                    },
                    "trait_id": {
                        "type": "string",
                        "description": "ID del tratto (per revise/retire/restore).",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Perché questa mutazione: cosa hai imparato o cosa è cambiato.",
                    },
                    "origin": {
                        "type": "string",
                        "description": "bootstrap | owner | learned (default learned).",
                    },
                },
                "required": ["action"],
            },
            handler=self._anima_update_handler,
        ))
        self.tools.register(Tool(
            name="remember",
            description=(
                "Salva un ricordo nella memoria semantica (ricerca per significato, "
                "non solo keyword). Usalo per fatti durevoli su utente, progetti, "
                "decisioni e lezioni che vuoi ritrovare nelle prossime sessioni."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "Il fatto da ricordare, formulato in modo autosufficiente.",
                    },
                    "category": {
                        "type": "string",
                        "description": "Categoria: user, project, lesson, decision, general.",
                    },
                },
                "required": ["content"],
            },
            handler=self._remember_handler,
        ))
        self.tools.register(Tool(
            name="reload_plugins",
            description="Ricarica i plugin da plugins/. Utile dopo aver creato o modificato un plugin.",
            parameters={
                "type": "object",
                "properties": {
                    "plugin_id": {
                        "type": "string",
                        "description": "ID del plugin da ricaricare. Vuoto = ricarica tutti.",
                    },
                },
            },
            handler=self._reload_plugins_handler,
        ))
        self.tools.register(Tool(
            name="list_plugins",
            description="Lista i plugin disponibili e il loro stato di caricamento.",
            parameters={"type": "object", "properties": {}},
            handler=self._list_plugins_handler,
        ))
        self.tools.register(Tool(
            name="request_restart",
            description="Richiede un restart del runtime tramite watcher quando hai modificato file Python che non possono essere ricaricati in-place.",
            parameters={
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "description": "Motivo del restart richiesto.",
                    },
                },
            },
            handler=self._request_restart_handler,
        ))
        self.tools.register(Tool(
            name="load_skill",
            description=(
                "Carica il contenuto completo di una skill (workflow, procedure, esempi). "
                "L'indice SKILLS nel system prompt elenca nome + descrizione di tutte le skill "
                "disponibili; quando una di quelle copre il task in corso, usa questo tool "
                "per ottenere la procedura completa prima di agire. "
                "Esempi: `coding` prima di un refactor non banale, `github` prima di creare PR, "
                "`progetto` per esplorare una codebase sconosciuta."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Nome esatto della skill come appare nell'indice SKILLS.",
                    },
                },
                "required": ["name"],
            },
            handler=self._load_skill_handler,
        ))
        self.tools.register(Tool(
            name="load_toolset",
            description=(
                "Attiva altri pacchetti di tool per il turno corrente quando il "
                "tool necessario non e' gia' disponibile. Pacchetti: files, web, "
                "memory, communication, runtime, agents, marketplace, all. Passa solo quelli "
                "necessari: gli schemi aggiuntivi aumentano i token di input."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "packs": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Uno o piu' pacchetti da attivare.",
                    },
                },
                "required": ["packs"],
            },
            handler=self._load_toolset_handler,
        ))
        self.tools.register(Tool(
            name="doctor",
            description="Diagnosi rapida del runtime: workspace, sandbox, plugin, audit e integrity.",
            parameters={"type": "object", "properties": {}},
            handler=self._doctor_handler,
        ))
        self.tools.register(Tool(
            name="doctor_fix",
            description="Bootstrap serio del runtime: scaffold memoria, ACL, audit e baseline integrity.",
            parameters={"type": "object", "properties": {}},
            handler=self._doctor_fix_handler,
        ))
        self.tools.register(Tool(
            name="capability_lease",
            description=(
                "Manage temporary approval leases for repeated sensitive actions. "
                "Use grant for a narrow command/path scope, list to inspect active leases, "
                "and revoke to remove one."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "description": "grant | list | revoke",
                    },
                    "tool_name": {
                        "type": "string",
                        "description": "Tool covered by the lease, for example shell, process_start, write_file",
                    },
                    "risk": {
                        "type": "string",
                        "description": "Maximum risk covered: safe, moderate, or high. Critical cannot be leased.",
                    },
                    "ttl_seconds": {
                        "type": "integer",
                        "description": "Lease lifetime in seconds, capped at 86400",
                    },
                    "max_uses": {
                        "type": "integer",
                        "description": "Maximum number of uses. 0 means unlimited until expiration.",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Why this lease is being granted",
                    },
                    "command_prefix": {
                        "type": "string",
                        "description": "Optional prefix required for command/text args",
                    },
                    "path_prefix": {
                        "type": "string",
                        "description": "Optional path prefix required for file args",
                    },
                    "lease_id": {
                        "type": "string",
                        "description": "Lease ID for revoke",
                    },
                    "include_expired": {
                        "type": "boolean",
                        "description": "If true, list expired or exhausted leases too",
                    },
                },
            },
            handler=self._capability_lease_handler,
        ))
        self.tools.register(Tool(
            name="agent_state",
            description=(
                "Inspect or update the current autonomy state: active goal, phase, "
                "observations, blockers, and final result."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "description": "status | note | block | finish | clear",
                    },
                    "note": {
                        "type": "string",
                        "description": "Observation, blocker, or final result text",
                    },
                    "waiting_user": {
                        "type": "boolean",
                        "description": "For finish: true if the task is waiting for user input",
                    },
                },
            },
            handler=self._agent_state_handler,
        ))
        self.tools.register(Tool(
            name="subagent_spawn",
            description="Avvia un sub-agente parallelo non bloccante. Supporta routing auto cloud/local/executor.",
            parameters={
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": "Task da delegare al sub-agente.",
                    },
                    "deliverable": {
                        "type": "string",
                        "description": "Deliverable richiesto al sub-agente.",
                    },
                    "model": {
                        "type": "string",
                        "description": "Modello opzionale per il sub-agente.",
                    },
                    "backend": {
                        "type": "string",
                        "description": "Backend opzionale del sub-agente (ollama/openai/groq/anthropic/openai_compatible).",
                    },
                    "thinking": {
                        "type": "string",
                        "description": "Thinking level opzionale del sub-agente.",
                    },
                    "mode": {
                        "type": "string",
                        "description": "auto | text | safe_executor | inherit_executor",
                    },
                    "timeout_seconds": {
                        "type": "integer",
                        "description": "Timeout massimo del sub-agente.",
                    },
                    "announce_back": {
                        "type": "boolean",
                        "description": "Se true, annuncia automaticamente il risultato al requester.",
                    },
                },
                "required": ["task"],
            },
            handler=self._subagent_spawn_handler,
        ))
        self.tools.register(Tool(
            name="subagent_list",
            description="Lista i sub-agenti lanciati in questa sessione.",
            parameters={"type": "object", "properties": {}},
            handler=self._subagent_list_handler,
        ))
        self.tools.register(Tool(
            name="subagent_wait",
            description="Attende il risultato di un sub-agente specifico.",
            parameters={
                "type": "object",
                "properties": {
                    "subagent_id": {
                        "type": "string",
                        "description": "ID del sub-agente.",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Timeout in secondi.",
                    },
                },
                "required": ["subagent_id"],
            },
            handler=self._subagent_wait_handler,
        ))
        self.tools.register(Tool(
            name="subagent_wait_all",
            description="Attende tutti i sub-agenti attivi e ritorna i risultati.",
            parameters={
                "type": "object",
                "properties": {
                    "timeout": {
                        "type": "integer",
                        "description": "Timeout in secondi per ciascun sub-agente.",
                    },
                },
            },
            handler=self._subagent_wait_all_handler,
        ))
        self._register_swarm_tools()
        self.tools.register(Tool(
            name="subagent_kill",
            description="Termina un sub-agente ancora in esecuzione.",
            parameters={
                "type": "object",
                "properties": {
                    "subagent_id": {
                        "type": "string",
                        "description": "ID del sub-agente.",
                    },
                },
                "required": ["subagent_id"],
            },
            handler=self._subagent_kill_handler,
        ))
        # Self-evolution tools
        for tool in [EVOLVE_SELF_TOOL, READ_SELF_TOOL]:
            self.tools.register(tool)
        # Notify (Telegram)
        self.tools.register(NOTIFY_TOOL)
        self.tools.register(NOTIFY_VOICE_TOOL)
        self.tools.register(NOTIFY_FILE_TOOL)
        self.tools.register(NOTIFY_PHOTO_TOOL)
        self.tools.register(NOTIFY_POLL_TOOL)
        # Scheduler (messaggi programmati)
        self.tools.register(SCHEDULE_NOTIFY_TOOL)
        self.tools.register(LIST_SCHEDULE_TOOL)
        self.tools.register(CANCEL_SCHEDULE_TOOL)
        # Voice tools (se disponibile)
        if _HAS_VOICE:
            if _config_bool("VOICE_TOOLS_ENABLED", _config_bool("VOICE_ENABLED", False)):
                self.tools.register(SPEAK_TOOL)
                self.tools.register(LIST_VOICES_TOOL)
            if _config_bool("MIC_ENABLED", False):
                self.tools.register(LISTEN_MIC_TOOL)
        # Chrome DevTools MCP (se disponibile)
        if _HAS_DEVTOOLS:
            self.tools.register(BROWSER_DEVTOOLS_TOOL)

    def _load_plugin_tools(self, plugin_id: str = "") -> tuple[list[str], list[str]]:
        """Ricarica i tool plugin dal disco evitando collisioni con i builtin."""
        for name in list(self._plugin_tool_names):
            self.tools.unregister(name)
        self._plugin_tool_names.clear()

        self.plugin_mgr = PluginManager(PLUGINS_DIR)
        loaded_tools: list[str] = []
        failed: list[str] = []

        plugin_ids = [plugin_id] if plugin_id else self.plugin_mgr.discover()
        for current_plugin_id in plugin_ids:
            plugin = self.plugin_mgr.load(current_plugin_id)
            if not plugin.loaded:
                failed.append(current_plugin_id)
                continue

        for tool in self.plugin_mgr.get_tools():
            if tool.name in self._builtin_tool_names:
                failed.append(f"{tool.name}: collisione con tool builtin")
                continue
            self.tools.register(tool)
            self._plugin_tool_names.add(tool.name)
            loaded_tools.append(tool.name)

        return loaded_tools, failed

    def _load_toolset_handler(self, packs: list[str]) -> ToolResult:
        if not isinstance(packs, list) or not packs:
            return ToolResult.fail(
                "packs deve essere una lista non vuota.",
                error_type=ErrorType.VALIDATION,
            )
        names, unknown = self.tool_router.activate(packs, set(self.tools.names()))
        self._active_tool_names = names
        active = ", ".join(sorted(self.tool_router.selection.packs))
        suffix = f" Pacchetti sconosciuti: {', '.join(unknown)}." if unknown else ""
        return ToolResult.ok(
            f"Toolset aggiornato: {len(names)} tool; pacchetti attivi: {active}.{suffix}"
        )

    def _register_swarm_tools(self):
        """Tool dello sciame: l'agente convoca e interroga i suoi specialisti.

        Restano registrati anche a sciame disabilitato: meglio un messaggio che
        spiega come riattivarlo che un tool che sparisce senza motivo.
        """
        self.tools.register(Tool(
            name="swarm_spawn",
            description=(
                "Convoca un nuovo specialista persistente nel tuo sciame. Usalo "
                "quando hai un dubbio che qualcuno con un punto di vista dedicato "
                "chiarirebbe meglio (es. un revisore critico, un esperto di "
                "sicurezza, un avvocato del diavolo). Resta disponibile anche nei "
                "turni successivi."
            ),
            parameters={"type": "object", "properties": {
                "name": {"type": "string",
                         "description": "Nome breve con cui chiamarlo (es. 'revisore')."},
                "role": {"type": "string",
                         "description": "Cosa sa fare e da che angolo guarda il problema."},
                "instructions": {"type": "string",
                                 "description": "Istruzioni permanenti opzionali."},
                "backend": {"type": "string",
                            "description": "Backend opzionale; vuoto = come l'agente."},
                "model": {"type": "string",
                          "description": "Modello opzionale; vuoto = come l'agente."},
            }, "required": ["name", "role"]},
            # Convocare un agente non e' un'azione interna: nasce qualcuno che
            # resta, consuma budget e comparira' nella tua rubrica. Lo chiede.
            requires_approval=True,
            handler=self._swarm_spawn_handler,
        ))
        self.tools.register(Tool(
            name="swarm_ask",
            description=(
                "Fai una domanda a uno specialista dello sciame, oppure a più di "
                "uno insieme (ognuno risponde per sé, pareri indipendenti). "
                "Lascia 'name' vuoto per chiedere a tutti."
            ),
            parameters={"type": "object", "properties": {
                "name": {"type": "string",
                         "description": "Nome dello specialista; vuoto = tutti."},
                "question": {"type": "string", "description": "La domanda."},
            }, "required": ["question"]},
            handler=self._swarm_ask_handler,
        ))
        self.tools.register(Tool(
            name="swarm_discuss",
            description=(
                "Fa discutere gli specialisti TRA LORO su un argomento, a turni: "
                "dal secondo giro ognuno legge gli altri e dice dove non è "
                "d'accordo. Usalo quando il dubbio ha più risposte difendibili."
            ),
            parameters={"type": "object", "properties": {
                "topic": {"type": "string", "description": "Argomento da discutere."},
                "names": {"type": "array", "items": {"type": "string"},
                          "description": "Partecipanti; vuoto = tutti."},
                "rounds": {"type": "integer", "description": "Numero di giri (default 2)."},
            }, "required": ["topic"]},
            handler=self._swarm_discuss_handler,
        ))
        self.tools.register(Tool(
            name="swarm_list",
            description="Elenca gli specialisti attualmente nello sciame.",
            parameters={"type": "object", "properties": {}},
            handler=self._swarm_list_handler,
        ))
        self.tools.register(Tool(
            name="swarm_dismiss",
            description="Congeda uno specialista che non serve più.",
            parameters={"type": "object", "properties": {
                "name": {"type": "string", "description": "Nome dello specialista."},
            }, "required": ["name"]},
            handler=self._swarm_dismiss_handler,
        ))
        self.tools.register(Tool(
            name="swarm_transcript",
            description="Rilegge gli ultimi scambi avvenuti nello sciame.",
            parameters={"type": "object", "properties": {
                "limit": {"type": "integer", "description": "Quanti scambi (default 20)."},
            }},
            handler=self._swarm_transcript_handler,
        ))

    def _swarm(self):
        if self.swarm is None:
            raise RuntimeError(
                "Sciame non attivo. Imposta SWARM_ENABLED=true nel .env e riavvia."
            )
        return self.swarm

    def _swarm_spawn_handler(self, name: str, role: str, instructions: str = "",
                             backend: str = "", model: str = "") -> ToolResult:
        from core.swarm import SwarmError
        try:
            member = self._swarm().spawn(
                name, role, instructions, backend, model,
            )
        except (SwarmError, RuntimeError) as exc:
            return ToolResult.fail(str(exc), error_type=ErrorType.VALIDATION)
        self.ui.status(f"[sciame: convocato {member.name}]")
        return ToolResult.ok(
            f"Specialista convocato: {member.describe()}. "
            f"Ora puoi interrogarlo con swarm_ask(name='{member.name}', ...)."
        )

    def _swarm_ask_handler(self, question: str, name: str = "") -> ToolResult:
        from core.swarm import SwarmError
        try:
            swarm = self._swarm()
            if str(name or "").strip():
                answer = swarm.ask(name, question)
                return ToolResult.ok(f"[{name}] {answer}")
            replies = swarm.broadcast(question)
        except (SwarmError, RuntimeError) as exc:
            return ToolResult.fail(str(exc), error_type=ErrorType.VALIDATION)
        return ToolResult.ok("\n\n".join(
            f"[{who}] {text}" for who, text in replies.items()
        ))

    def _swarm_discuss_handler(self, topic: str, names: list | None = None,
                               rounds: int = 2) -> ToolResult:
        from core.swarm import Swarm, SwarmError
        try:
            transcript = self._swarm().discuss(
                topic, list(names) if names else None, rounds,
            )
        except (SwarmError, RuntimeError) as exc:
            return ToolResult.fail(str(exc), error_type=ErrorType.VALIDATION)
        return ToolResult.ok(Swarm.render_discussion(transcript))

    def _swarm_list_handler(self) -> ToolResult:
        try:
            return ToolResult.ok(self._swarm().roster_text())
        except RuntimeError as exc:
            return ToolResult.fail(str(exc), error_type=ErrorType.VALIDATION)

    def _swarm_dismiss_handler(self, name: str) -> ToolResult:
        from core.swarm import SwarmError
        try:
            removed = self._swarm().dismiss(name)
        except (SwarmError, RuntimeError) as exc:
            return ToolResult.fail(str(exc), error_type=ErrorType.VALIDATION)
        return ToolResult.ok(f"Congedato: {removed}.")

    def _swarm_transcript_handler(self, limit: int = 20) -> ToolResult:
        try:
            entries = self._swarm().transcript(limit)
        except RuntimeError as exc:
            return ToolResult.fail(str(exc), error_type=ErrorType.VALIDATION)
        if not entries:
            return ToolResult.ok("Nessuno scambio registrato nello sciame.")
        lines = [
            f"{e.get('at', '')[:19]} {e.get('from')} → {e.get('to')}: "
            f"{str(e.get('text', ''))[:400]}"
            for e in entries
        ]
        return ToolResult.ok("\n".join(lines))

    def _register_vurpub_tools(self):
        """Tool del bancone vurpub. search/pull = lettura; approve/share = owner."""
        self.tools.register(Tool(
            name="vurpub_search",
            description=("Cerca al bancone vurpub skill e soluzioni condivise da "
                         "altri agenti openvurp. Restituisce candidati, non li "
                         "attiva. Usa parole chiave del problema."),
            parameters={"type": "object", "properties": {
                "query": {"type": "string", "description": "parole chiave / problema"}},
                "required": ["query"]},
            handler=self._vurpub_search_handler,
        ))
        self.tools.register(Tool(
            name="vurpub_pull",
            description=("Pesca un'entry vurpub per id e la salva come CANDIDATO "
                         "inerte (non attivo). Passa la guardia di sicurezza; "
                         "mostra capacità richieste e motivi di eventuale rifiuto. "
                         "Si attiva solo quando l'owner dà l'ok a voce (es. "
                         "'approva <slug>'); allora — e solo allora — chiama "
                         "vurpub_approve. NON dire all'owner di digitare comandi."),
            parameters={"type": "object", "properties": {
                "entry_id": {"type": "string", "description": "es. official/example-skill"}},
                "required": ["entry_id"]},
            handler=self._vurpub_pull_handler,
        ))
        self.tools.register(Tool(
            name="vurpub_candidates",
            description="Elenca i candidati vurpub scaricati ma non ancora attivati.",
            parameters={"type": "object", "properties": {}},
            handler=self._vurpub_candidates_handler,
        ))
        self.tools.register(Tool(
            name="vurpub_approve",
            description=("Promuove un candidato a skill ATTIVA (l'agente la userà). "
                         "Solo owner. Ri-passa la guardia prima di attivare."),
            parameters={"type": "object", "properties": {
                "slug": {"type": "string", "description": "slug del candidato"}},
                "required": ["slug"]},
            requires_approval=True,
            handler=self._vurpub_approve_handler,
        ))
        self.tools.register(Tool(
            name="vurpub_reject",
            description="Scarta un candidato vurpub senza attivarlo.",
            parameters={"type": "object", "properties": {
                "slug": {"type": "string", "description": "slug del candidato"}},
                "required": ["slug"]},
            handler=self._vurpub_reject_handler,
        ))
        self.tools.register(Tool(
            name="vurpub_share",
            description=("Contribuisce una skill/soluzione al bancone vurpub via "
                         "Pull Request. PII strippata e gate locale prima dell'invio. "
                         "Solo owner. Usa SOLO dopo che l'owner ha concordato di condividere."),
            parameters={"type": "object", "properties": {
                "kind": {"type": "string", "description": "skill | solution"},
                "title": {"type": "string", "description": "titolo dell'entry"},
                "body": {"type": "string", "description": "il contenuto (istruzioni o ricetta)"},
                "tags": {"type": "string", "description": "tag separati da virgola"},
                "network": {"type": "string", "description": "domini di rete usati, separati da virgola (vuoto = nessuno)"}},
                "required": ["kind", "title", "body"]},
            requires_approval=True,
            handler=self._vurpub_share_handler,
        ))

    def _vurpub_search_handler(self, query: str = "") -> ToolResult:
        if self.vurpub is None:
            return ToolResult(success=False, output="", error="vurpub non disponibile.")
        ok, msg = self.vurpub.sync()
        if not ok:
            return ToolResult(success=False, output="", error=f"sync vurpub fallito: {msg}")
        rows = self.vurpub.search(query)
        if not rows:
            return ToolResult(success=True, output="Nessun risultato al bancone.")
        lines = [f"[{r['trust']}] {r['id']} — {r['title']}\n    {r['description']}" for r in rows]
        return ToolResult(success=True, output="Risultati vurpub:\n" + "\n".join(lines))

    def _vurpub_pull_handler(self, entry_id: str = "") -> ToolResult:
        if self.vurpub is None:
            return ToolResult(success=False, output="", error="vurpub non disponibile.")
        self.vurpub.sync()
        res = self.vurpub.save_candidate(entry_id)
        st = res.get("status")
        if st == "candidate":
            caps = res.get("capabilities", {})
            return ToolResult(success=True, output=(
                f"Candidato salvato (inerte): {res['id']} — {res.get('title','')}\n"
                f"Tier: {res.get('trust')}  |  capacità richieste: {caps}\n"
                f"Resta inattivo finché non mi dai l'ok: dimmi «approva "
                f"{res.get('slug')}» (a voce, non è un comando) e la attivo io."))
        if st == "rejected":
            return ToolResult(success=False, output="",
                              error="Entry RIFIUTATA dalla guardia: " + "; ".join(res.get("reasons", [])))
        return ToolResult(success=False, output="", error=f"pull: {st}")

    def _vurpub_candidates_handler(self) -> ToolResult:
        if self.vurpub is None:
            return ToolResult(success=False, output="", error="vurpub non disponibile.")
        rows = self.vurpub.list_candidates()
        if not rows:
            return ToolResult(success=True, output="Nessun candidato in attesa.")
        lines = [f"{r['slug']} — {r['title']} [{r['trust']}] capacità={r['capabilities']}" for r in rows]
        return ToolResult(success=True, output="Candidati in attesa di approvazione:\n" + "\n".join(lines))

    def _vurpub_approve_handler(self, slug: str = "") -> ToolResult:
        if self.vurpub is None:
            return ToolResult(success=False, output="", error="vurpub non disponibile.")
        res = self.vurpub.approve(slug)
        if res.get("status") == "active":
            return ToolResult(success=True, output=f"Skill attivata: {res['path']}")
        if res.get("status") == "rejected":
            return ToolResult(success=False, output="",
                              error="Guardia ha bloccato l'attivazione: " + "; ".join(res.get("reasons", [])))
        return ToolResult(success=False, output="", error=f"approve: {res.get('status')}")

    def _vurpub_reject_handler(self, slug: str = "") -> ToolResult:
        if self.vurpub is None:
            return ToolResult(success=False, output="", error="vurpub non disponibile.")
        res = self.vurpub.reject(slug)
        return ToolResult(success=True, output=f"Candidato {slug}: {res.get('status')}")

    def _vurpub_share_handler(self, kind: str = "skill", title: str = "", body: str = "",
                              tags: str = "", network: str = "") -> ToolResult:
        if self.vurpub is None:
            return ToolResult(success=False, output="", error="vurpub non disponibile.")
        taglist = [t.strip() for t in (tags or "").split(",") if t.strip()]
        netlist = [n.strip() for n in (network or "").split(",") if n.strip()]
        caps = {"shell": False, "file_read": False, "file_write": False, "network": netlist}
        res = self.vurpub.share(kind=kind, title=title, body=body, tags=taglist, capabilities=caps)
        st = res.get("status")
        if st == "pr_open":
            return ToolResult(success=True, output=f"Contributo inviato come PR: {res['url']}")
        if st == "blocked":
            return ToolResult(success=False, output="",
                              error="Gate locale ha bloccato il contributo: " + "; ".join(res.get("reasons", [])))
        return ToolResult(success=False, output="", error=f"share: {st} — {res.get('error','')}")

    def _pact_handler(self, action: str = "", pact_type: str = "",
                      description: str = "", pattern: str = "",
                      reason: str = "", pact_id: str = "") -> ToolResult:
        if self.pacts is None:
            return ToolResult(success=False, output="", error="Patti non disponibili.")
        from core.pacts import PactError
        action = (action or "").strip().lower()
        try:
            if action == "add":
                pact = self.pacts.add(pact_type, description, pattern=pattern, reason=reason)
                return ToolResult(success=True, output=(
                    f"Patto stretto [{pact.id}] ({pact.pact_type}): {pact.description}. "
                    f"Il runtime lo farà rispettare."
                ))
            if action == "retire":
                pact = self.pacts.retire(pact_id, reason=reason)
                return ToolResult(success=True, output=f"Patto sciolto [{pact.id}].")
            if action == "list":
                return ToolResult(success=True, output=self.pacts.render_status())
            return ToolResult(success=False, output="",
                              error=f"Azione sconosciuta: {action}. Usa add/retire/list.")
        except PactError as e:
            return ToolResult(success=False, output="", error=str(e))

    def _curiosity_handler(self, action: str = "", question: str = "",
                           why: str = "", question_id: str = "",
                           summary: str = "") -> ToolResult:
        if self.curiosity is None:
            return ToolResult(success=False, output="", error="Curiosità non disponibile.")
        from core.curiosity import CuriosityError
        action = (action or "").strip().lower()
        try:
            if action == "add":
                q = self.curiosity.add(question, why=why)
                return ToolResult(success=True, output=(
                    f"Domanda registrata [{q.id}]: {q.question}"
                ))
            if action == "answer":
                q = self.curiosity.answer(question_id, summary)
                # Quello che impari studiando va anche in memoria semantica
                if summary:
                    self.memory.remember(
                        f"[curiosità] {q.question} → {summary}",
                        category="curiosity",
                    )
                return ToolResult(success=True, output=f"Studiata [{q.id}]: {q.question}")
            if action == "drop":
                q = self.curiosity.drop(question_id)
                return ToolResult(success=True, output=f"Lasciata cadere [{q.id}].")
            if action == "list":
                return ToolResult(success=True, output=self.curiosity.render_status())
            return ToolResult(success=False, output="",
                              error=f"Azione sconosciuta: {action}. Usa add/answer/drop/list.")
        except CuriosityError as e:
            return ToolResult(success=False, output="", error=str(e))

    def _project_handler(self, action: str = "", title: str = "", goal: str = "",
                         why: str = "", next_step: str = "", target_date: str = "",
                         project_id: str = "", note: str = "",
                         milestone: str = "", reason: str = "") -> ToolResult:
        if self.projects is None:
            return ToolResult(success=False, output="", error="Progetti non disponibili.")
        from core.projects import ProjectError
        action = (action or "").strip().lower()
        try:
            if action == "create":
                p = self.projects.create(title, goal, why=why,
                                         next_step=next_step,
                                         target_date=target_date)
                return ToolResult(success=True, output=(
                    f"Progetto avviato [{p.id}]: {p.title}. "
                    f"Lo terrò presente ogni giorno e lo avanzerò nei cicli autonomi."
                ))
            if action == "note":
                p = self.projects.note(project_id, note, next_step=next_step)
                out = f"Avanzamento registrato su [{p.id}] {p.title}."
                if next_step:
                    out += f" Prossimo passo: {p.next_step}"
                return ToolResult(success=True, output=out)
            if action == "set_next":
                p = self.projects.set_next_step(project_id, next_step)
                return ToolResult(success=True, output=f"Prossimo passo di [{p.id}]: {p.next_step}")
            if action == "milestone_add":
                p = self.projects.milestone_add(project_id, milestone)
                return ToolResult(success=True, output=f"Milestone aggiunta a [{p.id}]: {milestone}")
            if action == "milestone_done":
                p = self.projects.milestone_done(project_id, milestone)
                done, total = p.progress()
                return ToolResult(success=True, output=(
                    f"Milestone completata su [{p.id}] {p.title}: {done}/{total}."
                ))
            if action == "pause":
                p = self.projects.pause(project_id, reason=reason)
                return ToolResult(success=True, output=f"Progetto [{p.id}] in pausa.")
            if action == "resume":
                p = self.projects.resume(project_id)
                return ToolResult(success=True, output=f"Progetto [{p.id}] ripreso.")
            if action == "complete":
                p = self.projects.complete(project_id, outcome=note)
                return ToolResult(success=True, output=f"Progetto [{p.id}] {p.title} COMPLETATO.")
            if action == "drop":
                p = self.projects.drop(project_id, reason=reason)
                return ToolResult(success=True, output=f"Progetto [{p.id}] abbandonato.")
            if action == "list":
                return ToolResult(success=True, output=self.projects.render_status())
            return ToolResult(success=False, output="", error=(
                f"Azione sconosciuta: {action}. Usa create/note/set_next/"
                f"milestone_add/milestone_done/pause/resume/complete/drop/list."
            ))
        except ProjectError as e:
            return ToolResult(success=False, output="", error=str(e))

    def _forge_handler(self, action: str = "", plugin_id: str = "", need: str = "",
                       why: str = "", forge_id: str = "", reason: str = "") -> ToolResult:
        if self.forge is None:
            return ToolResult(success=False, output="", error="Fucina non disponibile.")
        from core.forge import ForgeError
        action = (action or "").strip().lower()
        source = getattr(self, "_current_tool_source", "cli")
        try:
            if action == "propose":
                origin = "heartbeat" if source in ("heartbeat", "cron") else "agent"
                e = self.forge.propose(plugin_id, need, why=why, origin=origin)
                return ToolResult(success=True, output=(
                    f"Lacuna registrata [{e.id}] per il plugin {e.plugin_id}. "
                    f"Ora scrivi il codice: scaffold_plugin, implementa l'handler "
                    f"E una funzione selftest() che prova che funziona, poi "
                    f"forge action=draft e action=test."
                ))
            if action == "draft":
                e = self.forge.mark_drafted(forge_id)
                return ToolResult(success=True, output=(
                    f"[{e.id}] in bozza. Prossimo: forge action=test "
                    f"(esegue selftest() in un processo isolato)."
                ))
            if action == "test":
                e = self.forge.test(forge_id)
                return ToolResult(success=True, output=(
                    f"[{e.id}] selftest PASSATO. La capacità è pronta: "
                    f"chiedi all'owner il via libera e poi forge action=adopt."
                ))
            if action == "adopt":
                e = self.forge.adopt(forge_id, source=source)
                # Carica il plugin appena adottato e registra i suoi tool
                loaded, failed = self._load_plugin_tools()
                out = (
                    f"Capacità adottata [{e.id}]: {e.plugin_id} è in servizio. "
                    f"Nata da: {e.need}"
                )
                if loaded:
                    out += f"\nTool ora disponibili: {', '.join(loaded)}"
                if failed:
                    out += f"\nAttenzione, caricamento fallito per: {failed}"
                return ToolResult(success=True, output=out)
            if action == "reject":
                e = self.forge.reject(forge_id, reason=reason)
                return ToolResult(success=True, output=f"[{e.id}] rifiutata.")
            if action == "retire":
                e = self.forge.retire(forge_id, reason=reason)
                return ToolResult(success=True, output=(
                    f"[{e.id}] ritirata: il plugin {e.plugin_id} è disattivato."
                ))
            if action == "list":
                return ToolResult(success=True, output=self.forge.render_status())
            return ToolResult(success=False, output="", error=(
                f"Azione sconosciuta: {action}. Usa propose/draft/test/"
                f"adopt/reject/retire/list."
            ))
        except ForgeError as e:
            return ToolResult(success=False, output="", error=str(e))

    def _sense_handler(self, action: str = "", kind: str = "", target: str = "",
                       label: str = "", why: str = "", sense_id: str = "") -> ToolResult:
        if self.senses is None:
            return ToolResult(success=False, output="", error="Sensi non disponibili.")
        from core.senses import SenseError
        action = (action or "").strip().lower()
        try:
            if action == "add":
                s = self.senses.add(kind, target, label, why=why)
                return ToolResult(success=True, output=(
                    f"Senso aperto [{s.id}] ({s.kind}): osserverò {s.target} "
                    f"tra un heartbeat e l'altro. Primo sguardo fatto in silenzio: "
                    f"da ora segnalo solo le novità."
                ))
            if action == "remove":
                s = self.senses.remove(sense_id)
                return ToolResult(success=True, output=f"Senso chiuso [{s.id}]: {s.label}.")
            if action == "list":
                return ToolResult(success=True, output=self.senses.render_status())
            return ToolResult(success=False, output="",
                              error=f"Azione sconosciuta: {action}. Usa add/remove/list.")
        except SenseError as e:
            return ToolResult(success=False, output="", error=str(e))

    def _follow_up_handler(self, action: str = "", what: str = "", due: str = "",
                           why: str = "", thread_id: str = "",
                           outcome: str = "") -> ToolResult:
        if self.bonds is None:
            return ToolResult(success=False, output="", error="Legame non disponibile.")
        from core.bonds import BondError
        action = (action or "").strip().lower()
        try:
            if action == "add":
                t = self.bonds.add_thread(what, due, why=why)
                return ToolResult(success=True, output=(
                    f"Filo legato [{t.id}]: {t.what}. "
                    f"Al momento giusto ({t.due[:16].replace('T', ' ')}) "
                    f"scriverò io per chiedere com'è andata."
                ))
            if action == "asked":
                t = self.bonds.mark_asked(thread_id)
                return ToolResult(success=True, output=(
                    f"Segnato [{t.id}]: ho chiesto, aspetto la risposta dell'owner."
                ))
            if action == "close":
                t = self.bonds.close_thread(thread_id, outcome=outcome)
                out = f"Filo chiuso [{t.id}]: {t.what}."
                # L'esito è un ricordo che vale: va in memoria semantica
                if outcome:
                    self.memory.remember(
                        f"[filo] {t.what} → {outcome}", category="bonds",
                    )
                return ToolResult(success=True, output=out)
            if action == "list":
                return ToolResult(success=True, output=self.bonds.render_status())
            return ToolResult(success=False, output="",
                              error=f"Azione sconosciuta: {action}. Usa add/asked/close/list.")
        except BondError as e:
            return ToolResult(success=False, output="", error=str(e))

    def _second_opinion_handler(self, question: str = "", context: str = "") -> ToolResult:
        """Chiede un parere a un modello diverso prima di rispondere su
        cose importanti. Usa il modello profondo di escalation."""
        question = (question or "").strip()
        if len(question) < 10:
            return ToolResult(success=False, output="",
                              error="Formula una domanda vera per la seconda opinione.")
        try:
            from core.escalation import resolve_deep_model
            backend, model = resolve_deep_model()
            if not backend or not model:
                return ToolResult(success=False, output="", error=(
                    "Nessun modello configurato per la seconda opinione: "
                    "imposta ESCALATION_DEEP_BACKEND e ESCALATION_DEEP_MODEL in .env."
                ))
            if self.budget is not None and self.budget.over_budget():
                return ToolResult(success=False, output="",
                                  error="Budget LLM giornaliero esaurito.")
            if self._deep_llm is None or (
                self._deep_llm.backend != backend or self._deep_llm.model != model
            ):
                self._deep_llm = create_llm_client(backend=backend, model=model)
            prompt = (
                "Sei un consulente esterno: dai un parere indipendente, diretto "
                "e onesto. Se vedi rischi o alternative migliori, dillo.\n\n"
            )
            if context.strip():
                prompt += f"Contesto:\n{context.strip()[:4000]}\n\n"
            prompt += f"Domanda:\n{question[:2000]}"
            if self.budget is not None:
                self.budget.record_call()
            answer = self._deep_llm.call([{"role": "user", "content": prompt}])
            return ToolResult(success=True, output=(
                f"Seconda opinione ({model}):\n{answer}"
            ))
        except Exception as e:
            return ToolResult(success=False, output="",
                              error=f"Seconda opinione non disponibile: {e}")

    def _anima_update_handler(self, action: str = "", section: str = "",
                              text: str = "", trait_id: str = "",
                              reason: str = "", origin: str = "learned") -> ToolResult:
        if self.anima is None:
            return ToolResult(success=False, output="", error="Anima non disponibile.")
        from core.anima import AnimaError
        action = (action or "").strip().lower()
        try:
            if action == "add":
                trait = self.anima.add_trait(section, text, origin=origin, reason=reason)
                return ToolResult(success=True, output=(
                    f"Tratto nato [{trait.id}] in '{trait.section}': {trait.text}"
                ))
            if action == "revise":
                trait = self.anima.revise_trait(trait_id, text, reason=reason)
                return ToolResult(success=True, output=(
                    f"Tratto rivisto [{trait.id}] v{trait.version}: {trait.text}"
                ))
            if action == "retire":
                trait = self.anima.retire_trait(trait_id, reason=reason)
                return ToolResult(success=True, output=f"Tratto ritirato [{trait.id}].")
            if action == "restore":
                trait = self.anima.restore_trait(trait_id, reason=reason)
                return ToolResult(success=True, output=f"Tratto ripristinato [{trait.id}].")
            return ToolResult(success=False, output="",
                              error=f"Azione sconosciuta: {action}. Usa add/revise/retire/restore.")
        except AnimaError as e:
            return ToolResult(success=False, output="", error=str(e))

    def _remember_handler(self, content: str = "", category: str = "general") -> ToolResult:
        if not (content or "").strip():
            return ToolResult(success=False, output="", error="content vuoto")
        ok = self.memory.remember(content, category=category or "general")
        if ok:
            return ToolResult(success=True, output=f"Ricordo salvato ({category or 'general'}).")
        return ToolResult(
            success=False, output="",
            error="Memoria semantica non disponibile (VECTOR_MEMORY_ENABLED=false o init fallita).",
        )

    def _list_plugins_handler(self) -> ToolResult:
        plugins = self.plugin_mgr.list_plugins()
        if not plugins:
            return ToolResult.ok("No plugins available in plugins/.")
        return ToolResult.ok(json.dumps(plugins, indent=2, ensure_ascii=False))

    def _reload_plugins_handler(self, plugin_id: str = "") -> ToolResult:
        loaded_tools, failed = self._load_plugin_tools((plugin_id or "").strip())
        self._build_system_prompt()

        details = []
        if loaded_tools:
            details.append(f"Tool plugin caricati: {', '.join(sorted(loaded_tools))}")
        if failed:
            details.append(f"Problemi: {', '.join(failed)}")

        if plugin_id and not loaded_tools:
            message = "\n".join(details) if details else f"Plugin {plugin_id} non caricato."
            return ToolResult.fail(message or f"Plugin {plugin_id} non caricato.")

        return ToolResult.ok("\n".join(details) if details else "Plugin ricaricati.")

    def _request_restart_handler(self, reason: str = "") -> ToolResult:
        restart_reason = (reason or "").strip() or "Restart richiesto da tool request_restart"
        sentinel = os.path.join(MEMORY_DIR, ".restart")
        os.makedirs(os.path.dirname(sentinel), exist_ok=True)
        with open(sentinel, "w", encoding="utf-8") as f:
            f.write(f"{time.time()}\n{restart_reason}\n")
        return ToolResult.ok(
            "Restart requested. If running under the watcher, the runtime will restart automatically."
        )

    def _capability_lease_handler(self, action: str = "list",
                                  tool_name: str = "",
                                  risk: str = "high",
                                  ttl_seconds: int = 600,
                                  max_uses: int = 5,
                                  reason: str = "",
                                  command_prefix: str = "",
                                  path_prefix: str = "",
                                  lease_id: str = "",
                                  include_expired: bool = False) -> ToolResult:
        action = (action or "list").strip().lower()

        try:
            if action in ("grant", "add", "create"):
                lease = self.capability_leases.grant(
                    actor=self._active_actor_id,
                    source=self._active_channel,
                    tool_name=tool_name,
                    risk=risk,
                    ttl_seconds=ttl_seconds,
                    max_uses=max_uses,
                    reason=reason,
                    command_prefix=command_prefix,
                    path_prefix=path_prefix,
                    metadata={"session_key": self._active_route.session_key},
                )
                return ToolResult.ok(json.dumps(
                    {"granted": lease.to_public_dict()},
                    indent=2,
                    ensure_ascii=False,
                    sort_keys=True,
                ))

            if action in ("revoke", "delete"):
                if not (lease_id or "").strip():
                    return ToolResult.fail("lease_id is required", error_type=ErrorType.VALIDATION)
                removed = self.capability_leases.revoke(lease_id.strip())
                if not removed:
                    return ToolResult.fail(
                        f"Lease not found: {lease_id}",
                        error_type=ErrorType.NOT_FOUND,
                    )
                return ToolResult.ok(f"Revoked lease {lease_id.strip()}")

            if action in ("list", "show"):
                leases = [
                    lease.to_public_dict()
                    for lease in self.capability_leases.list_leases(
                        include_expired=include_expired
                    )
                ]
                return ToolResult.ok(json.dumps(
                    {"leases": leases},
                    indent=2,
                    ensure_ascii=False,
                    sort_keys=True,
                ))

            if action == "prune":
                removed = self.capability_leases.prune()
                return ToolResult.ok(f"Pruned {removed} expired or exhausted lease(s)")

            return ToolResult.fail(
                f"Unknown action: {action}",
                error_type=ErrorType.VALIDATION,
            )
        except ValueError as exc:
            return ToolResult.fail(str(exc), error_type=ErrorType.VALIDATION)
        except Exception as exc:
            return ToolResult.fail(str(exc), error_type=ErrorType.RUNTIME)

    def _agent_state_handler(self, action: str = "status",
                             note: str = "",
                             waiting_user: bool = False) -> ToolResult:
        action = (action or "status").strip().lower()
        try:
            if action in ("status", "show", "list"):
                return ToolResult.ok(json.dumps(
                    self.agent_state.status(),
                    indent=2,
                    ensure_ascii=False,
                    sort_keys=True,
                ))
            if action in ("note", "observe"):
                if not note.strip():
                    return ToolResult.fail("note is required", error_type=ErrorType.VALIDATION)
                task = self.agent_state.add_note(note)
                if not task:
                    return ToolResult.fail("No active task.", error_type=ErrorType.NOT_FOUND)
                return ToolResult.ok("Observation recorded.")
            if action in ("block", "blocked"):
                if not note.strip():
                    return ToolResult.fail("note is required", error_type=ErrorType.VALIDATION)
                self.agent_state.fail(note, phase=AgentPhase.BLOCKED)
                return ToolResult.ok("Blocker recorded.")
            if action in ("finish", "complete", "done"):
                self.agent_state.finish(note, waiting_user=waiting_user)
                return ToolResult.ok("Task state updated.")
            if action == "clear":
                self.agent_state.clear()
                return ToolResult.ok("Agent state cleared.")
            return ToolResult.fail(f"Unknown action: {action}", error_type=ErrorType.VALIDATION)
        except Exception as exc:
            return ToolResult.fail(str(exc), error_type=ErrorType.RUNTIME)

    def _load_skill_handler(self, name: str = "") -> ToolResult:
        skill_name = (name or "").strip()
        if not skill_name:
            return ToolResult.fail(
                "Nome skill mancante.",
                error_type=ErrorType.VALIDATION,
            )
        skill = self.context_mgr.load_skill(skill_name)
        if not skill:
            available = ", ".join(
                sorted(s.name for s in self.context_mgr.get_all_skills())
            ) or "(nessuna)"
            return ToolResult.fail(
                f"Skill '{skill_name}' non trovata. Disponibili: {available}",
                error_type=ErrorType.NOT_FOUND,
            )
        header = f"# skill: {skill.name}\n"
        if skill.description:
            header += f"> {skill.description}\n\n"
        return ToolResult.ok(header + skill.content)

    def _doctor_handler(self) -> ToolResult:
        report = build_doctor_report(OPENVURP_DIR, self.tools.names())
        return ToolResult.ok(report.render())

    def _doctor_fix_handler(self) -> ToolResult:
        try:
            import config as cfg
            allowed_users = list(getattr(cfg, "TELEGRAM_ALLOWED_USERS", []) or [])
        except Exception:
            allowed_users = []
        report = fix_runtime_issues(OPENVURP_DIR, allowed_telegram_users=allowed_users)
        self.rbac = RBAC(MEMORY_DIR)
        return ToolResult.ok(report.render())

    def _subagent_spawn_handler(self, task: str, deliverable: str = "",
                                model: str = "", thinking: str = "",
                                backend: str = "", mode: str = "",
                                timeout_seconds: int = 0,
                                announce_back: bool = True) -> ToolResult:
        task = (task or "").strip()
        if not task:
            return ToolResult.fail("Task sub-agente vuoto.", error_type=ErrorType.VALIDATION)

        run = self.subagent_mgr.spawn(
            task=task,
            model=(model or None),
            thinking=(thinking or None),
            deliverable=(deliverable or ""),
            requested_by=self._active_actor_id,
            backend=(backend or None),
            mode=(mode or ""),
            timeout_seconds=timeout_seconds or 0,
            announce_back=announce_back,
            request_route=self._active_route,
        )
        if run.status.value == "failed":
            return ToolResult.fail(run.error or "Sub-agente fallito.")

        return ToolResult.ok(json.dumps({
            "id": run.id,
            "status": run.status.value,
            "task": run.task,
            "deliverable": run.deliverable,
            "requested_by": run.requested_by,
            "backend": run.backend,
            "model": run.model,
            "mode": run.mode,
            "strategy": run.strategy,
            "routing_reason": run.routing_reason,
            "timeout_seconds": run.timeout_seconds,
            "parent_session_key": run.parent_session_key,
            "child_session_key": run.child_session_key,
        }, ensure_ascii=False, indent=2))

    def _subagent_list_handler(self) -> ToolResult:
        return ToolResult.ok(json.dumps(self.subagent_mgr.list_children(), ensure_ascii=False, indent=2))

    def _subagent_wait_handler(self, subagent_id: str, timeout: int = 120) -> ToolResult:
        subagent_id = (subagent_id or "").strip()
        if not subagent_id:
            return ToolResult.fail("subagent_id mancante.", error_type=ErrorType.VALIDATION)
        return ToolResult.ok(self.subagent_mgr.wait(subagent_id, timeout=timeout))

    def _subagent_wait_all_handler(self, timeout: int = 300) -> ToolResult:
        return ToolResult.ok(json.dumps(
            self.subagent_mgr.wait_all(timeout=timeout),
            ensure_ascii=False,
            indent=2,
        ))

    def _subagent_kill_handler(self, subagent_id: str) -> ToolResult:
        subagent_id = (subagent_id or "").strip()
        if not subagent_id:
            return ToolResult.fail("subagent_id mancante.", error_type=ErrorType.VALIDATION)
        if not self.subagent_mgr.kill(subagent_id):
            return ToolResult.fail(
                f"Sub-agente {subagent_id} non trovato.",
                error_type=ErrorType.NOT_FOUND,
            )
        return ToolResult.ok(f"Sub-agente {subagent_id} terminato.")

    def _init_mcp(self):
        """Inizializza client MCP se configurato."""
        mcp_config = os.path.join(OPENVURP_DIR, "mcp.json")
        if os.path.exists(mcp_config):
            try:
                from core.mcp import MCPClient
                self._mcp_client = MCPClient.from_config(mcp_config)
                # Connetti tutti i server
                for name in list(self._mcp_client.servers.keys()):
                    if self._mcp_client.connect(name):
                        # Registra tool MCP come tool openvurp
                        from core.tools import Tool
                        for mcp_tool in self._mcp_client.servers[name].tools:
                            tool = Tool(
                                name=f"mcp_{name}_{mcp_tool.get('name', 'unknown')}",
                                description=mcp_tool.get("description", "MCP tool"),
                                parameters=mcp_tool.get("inputSchema", {}),
                            )
                            self.tools.register(tool)
            except Exception:
                pass

    def _build_system_prompt(self, user_input: str = "",
                             source: str = "cli", sender: str = "user"):
        """Costruisce/aggiorna il system prompt con personalità iniettata.

        Approccio a livelli: i file workspace vengono riletti da disco ogni turno.
        Modifiche fatte dall'agente (via evolve_self) o dall'utente sono immediate.
        """
        # 1. Carica file workspace freschi da disco (con stat-based caching)
        session_type = resolve_session_type(source, sender, self._active_chat_type)
        bootstrap_files = self.bootstrap.load_all(session_type=session_type)

        # Anima attiva → l'identità compilata sostituisce i file markdown
        anima_text = ""
        if self.anima is not None and session_type != "heartbeat":
            try:
                if self.anima.active():
                    anima_text = self.anima.compile_prompt(session_type)
                    bootstrap_files = [
                        f for f in bootstrap_files
                        if f.name not in ("SOUL.md", "IDENTITY.md", "USER.md")
                    ]
            except Exception:
                anima_text = ""

        bootstrap_context = self.bootstrap.build_project_context(bootstrap_files)
        if anima_text:
            bootstrap_context = (
                anima_text + ("\n\n" + bootstrap_context if bootstrap_context else "")
            )

        # 2. Memoria strutturata (keyword retrieval)
        environment_text = ""
        try:
            snapshot = self.environment.refresh_memory(self.memory)
            environment_text = render_environment_prompt(snapshot)
        except Exception:
            pass

        # 3. Memoria strutturata (keyword retrieval)
        # Allineato ai file workspace a livelli:
        # la memoria dinamica vive solo nella sessione privata principale.
        if session_type != "main":
            memory_text = ""
        else:
            try:
                import config as cfg
                memory_budget = int(getattr(cfg, "MEMORY_RETRIEVAL_CHARS", 3000))
            except Exception:
                memory_budget = 3000
            memory_text = self.memory.get_relevant(
                user_input, budget_chars=max(500, memory_budget),
                session_type=session_type,
            )

        # 4. Schema tool
        native_tools = getattr(
            self._active_llm, "supports_tool_transport",
            self._active_llm.supports_function_calling,
        )
        tools_section = self.tools.prompt_section(
            native_tools=native_tools,
            names=self._active_tool_names,
        )
        # I backend CLI (Codex/Claude) passano gli schemi come dynamic tools,
        # ma quel canale puo' mancare. Senza un indice testuale il modello non
        # saprebbe nemmeno di avere dei tool: e' cosi' che l'agente e' diventato
        # un chatbot che descrive le azioni invece di eseguirle.
        if not tools_section and self._active_llm.backend in ("codex", "claude_cli"):
            tools_section = self.tools.compact_index(names=self._active_tool_names)
        method_text = build_operating_method(
            snapshot, sorted(self._active_tool_names)
        ) if environment_text else ""
        sensory_tools = {
            "web_search", "web_fetch", "browser", "browser_devtools",
            "image_analyze", "desktop_screenshot", "audio_transcribe",
            "listen_mic", "speak", "pdf_read", "notify_file", "notify_photo",
        }
        capability_text = ""
        if self._active_tool_names.intersection(sensory_tools):
            capability_text = render_capability_prompt(
                inspect_runtime_capabilities(sorted(self._active_tool_names))
            )

        # 5. Costruisci system prompt con bootstrap context iniettato
        system = self.context_mgr.build_system_prompt(
            bootstrap_context=bootstrap_context,
            memory_text=memory_text,
            tools_section=tools_section,
            user_input=user_input,
            environment_text=environment_text,
            method_text=method_text,
            native_tools=native_tools,
        )
        if capability_text:
            system += "\n\n" + capability_text

        try:
            import config as cfg
            continuity_budget = int(getattr(cfg, "CONTINUITY_PROMPT_CHARS", 2000))
        except Exception:
            continuity_budget = 2000
        continuity_text = self.continuity.build(
            user_input=user_input,
            session_type=session_type,
            budget_chars=max(500, continuity_budget),
        ) if self.continuity else ""
        if continuity_text:
            system += "\n\n" + continuity_text

        # Progetti attivi: la direzione a lungo termine resta presente
        # in ogni turno della sessione principale.
        if self.projects is not None and session_type == "main":
            try:
                projects_text = self.projects.compile_prompt()
                if projects_text:
                    system += "\n\n" + projects_text
            except Exception:
                pass

        kernel_text = self.kernel.prompt_section(self._active_kernel_plan)
        if kernel_text:
            system += "\n\n" + kernel_text

        # Sciame: dirlo esplicitamente, altrimenti i tool esistono ma non
        # vengono mai usati — un dubbio si risolve tirando a indovinare.
        if self.swarm is not None and session_type == "main":
            roster = self.swarm.roster_text()
            system += (
                "\n\n## IL TUO SCIAME\n"
                "Puoi convocare da solo degli specialisti persistenti e parlarci. "
                "Fallo quando hai un dubbio vero: due letture difendibili dello "
                "stesso problema, una decisione rischiosa, una tua conclusione che "
                "vorresti veder contestata prima di consegnarla.\n"
                "- `swarm_spawn` per convocarne uno (dagli un ruolo preciso e un "
                "angolo diverso dal tuo: non serve un secondo te stesso).\n"
                "- `swarm_ask` per una domanda; senza nome la ricevono tutti e "
                "rispondono in modo indipendente.\n"
                "- `swarm_discuss` per farli discutere fra loro a turni quando "
                "il disaccordo è la parte utile.\n"
                "- `swarm_list` / `swarm_dismiss` per gestirli.\n"
                "Riporta all'utente la conclusione e da dove nasce, non la "
                "trascrizione integrale. Non convocare nessuno per domande "
                "semplici: lì lo sciame è solo latenza e token.\n"
                f"{roster}"
            )

        # Riflesso vurpub: davanti a un problema non banale, guarda al bancone
        # PRIMA di improvvisare. Solo in sessione privata (non nei gruppi) e solo
        # se il bancone è collegato (resta una feature del branch vurpub).
        if getattr(self, "vurpub", None) is not None and session_type == "main":
            system += (
                "\n\n## IL BANCONE (vurpub)\n"
                "Per problemi non banali puoi cercare skill/soluzioni condivise. "
                "Una skill si legge e si segue; una soluzione si applica al task: "
                "non si eseguono come programmi e non richiedono di riscrivere il runtime. "
                "Flusso: search → pull candidato → approvazione owner → applicazione. "
                "Non cercare per cose banali e non attivare mai candidati da solo."
            )

        # 6. Personality enhancement — anti-narrazione, silenzio, self-evolution, voice primer
        system = enhance_system_prompt(
            system,
            backend=self._active_llm.backend,
            supports_native_tools=native_tools,
            is_group=self._active_chat_type in ("group", "supergroup", "channel"),
            include_proactivity=(
                source in ("heartbeat", "cron") or "follow_up" in self._active_tool_names
            ),
            include_growth=bool(self._active_tool_names.intersection({
                "evolve_self", "forge", "scaffold_plugin", "reload_plugins",
            })),
        )

        # 6b. Modalità operativa
        mode = getattr(self, "approval_mode", "safe")
        if mode == "plan":
            system += (
                "\n\n## MODALITÀ: PLAN\n"
                "Sei in plan mode: puoi solo osservare (leggere file, cercare, "
                "ispezionare). Ogni azione che modificherebbe qualcosa è bloccata "
                "dal runtime. Costruisci un piano numerato, concreto e completo, "
                "presentalo all'utente e fermati. Non fingere di aver eseguito."
            )
        elif mode == "auto":
            system += (
                "\n\n## MODALITÀ: AUTO\n"
                "Le azioni non critiche sono pre-approvate: lavora con slancio, "
                "senza chiedere conferme inutili. I comandi critici restano "
                "bloccati dal runtime. Resta prudente sulle azioni esterne."
            )

        # 7. Dove sei adesso — l'agente deve SAPERE il suo contesto per comportarsi
        # di conseguenza: terminale col suo owner, chat privata 1-a-1, o gruppo
        # con più persone. Senza questo non può distinguere "Pico?" rivolto a lui
        # da una chiacchiera tra altri.
        venue = describe_venue(source, self._active_chat_type, sender,
                               self._active_addressed)
        if venue:
            system += "\n\n" + venue

        # 7b. Contesto riavvio
        if self._restart_context:
            system += (
                f"\n\n## RIAVVIO RECENTE\n"
                f"Ti sei appena riavviato. Motivo: {self._restart_context}\n"
                f"La conversazione precedente è stata ripristinata — "
                f"continua come se non fosse successo nulla. "
                f"NON dire all'utente che ti sei riavviato, NON salutare di nuovo. "
                f"Rispondi normalmente, come se la chat non si fosse mai interrotta."
            )

        # 8. Info piano attivo
        if self.active_plan:
            system += "\n\n" + self.active_plan.summary_for_prompt()

        turn_context = str(getattr(self, "_active_turn_context", "") or "").strip()
        if turn_context:
            system += "\n\n" + turn_context

        if self.messages and self.messages[0]["role"] == "system":
            self.messages[0]["content"] = system
        else:
            self.messages.insert(0, {"role": "system", "content": system})

    def _get_session_messages(self, route: SessionRoute | None = None,
                              source: str = "cli", sender: str = "user") -> list[dict]:
        """Ottieni la lista messaggi per una sessione specifica."""
        route = route or self._build_route(source=source, sender=sender)
        if route.source == "cli" and route.session_key == "cli:main":
            return self.messages

        key = route.session_key
        if key not in self._channel_sessions:
            # Ripristina la cronologia durevole della route. Il system prompt
            # viene sempre rigenerato fresco e non e' duplicato su disco.
            self._channel_sessions[key] = self.session_store.load_messages(key)
            # Copia system prompt dalla sessione CLI
            if self.messages and self.messages[0]["role"] == "system":
                self._channel_sessions[key].insert(0, dict(self.messages[0]))
        return self._channel_sessions[key]

    def _set_session_messages(self, messages: list[dict], route: SessionRoute | None = None,
                              source: str = "cli", sender: str = "user"):
        """Aggiorna la lista messaggi per una sessione."""
        route = route or self._build_route(source=source, sender=sender)
        if route.source == "cli" and route.session_key == "cli:main":
            self.messages = messages
        else:
            self._channel_sessions[route.session_key] = messages
            self.session_store.save_messages(route.session_key, messages)

    def _trim_messages(self, messages: list[dict]) -> list[dict]:
        """Gestione contesto a livelli a 4 livelli."""
        messages = self.context_mgr.prune_messages(messages)
        tools_schema = self._get_tools_schema()
        try:
            import config as cfg
            target = int(getattr(cfg, "CONTEXT_TARGET_TOKENS", 24000))
        except Exception:
            target = 24000
        schema_tokens = self.context_mgr.schema_chars(tools_schema) // 4
        messages = self.context_mgr.prune_to_target(
            messages, max(4000, target - schema_tokens),
        )
        if self.context_mgr.should_compact(messages, tools_schema):
            messages = self._do_llm_compaction(messages)
        return messages

    def _do_llm_compaction(self, messages: list[dict]) -> list[dict]:
        """Compaction via LLM."""
        try:
            budget = self.context_mgr.check_budget(messages)
            self.observer.log_event("compaction_start", {
                "total_tokens": budget["total_tokens"],
                "ratio": budget["ratio"],
            })

            compact_msgs = self.context_mgr.build_compaction_prompt(messages)
            summary = self._active_llm.call(compact_msgs)

            if summary and len(summary) > 50:
                messages = self.context_mgr.apply_compaction(messages, summary)
                self.observer.log_event("compaction_done", {
                    "before_tokens": budget["total_tokens"],
                    "after_tokens": self.context_mgr.check_budget(messages)["total_tokens"],
                })
            else:
                system = [m for m in messages if m.get("role") == "system"]
                others = [m for m in messages if m.get("role") != "system"]
                messages = system + others[-10:]
        except Exception as e:
            self.observer.log_error(str(e), "compaction_failed")
            system = [m for m in messages if m.get("role") == "system"]
            others = [m for m in messages if m.get("role") != "system"]
            messages = system + others[-10:]

        return messages

    def run(self, user_input: str, source: str = "cli", sender: str = "user",
            actor_id: str = "cli_owner", chat_id: str = "", thread_id: str = "",
            session_key: str = "", parent_session_key: str = "",
            addressed: bool = True, chat_type: str = "", turn_context: str = "",
            llm_backend: str = "", llm_model: str = ""):
        """Main loop per un turno utente.

        Args:
            source: "cli", "telegram", "discord", etc.
            sender: nome del sender (per sessioni separate)
            actor_id: identità autorizzativa del chiamante
            addressed: True quando il messaggio è rivolto all'agente
                (privata sempre; gruppo solo se @menzione o reply al bot).
                In gruppo NON interpellato il runtime non dovrebbe chiamare
                run(), ma se lo fa per la modalità "natural/all" l'agente
                deve saperlo per non rispondere a caso.
        """
        import config as cfg

        # Budget autonomia: heartbeat e backend CLI hanno tetti più bassi dei
        # turni interattivi (vedi iteration_budget). Ricalcolato più sotto,
        # quando backend e modalità del turno sono noti.
        max_iter = self.iteration_budget(source=source or "cli")
        previous_actor = self._active_actor_id
        previous_channel = self._active_channel
        previous_route = self._active_route
        previous_runtime_session = self._active_runtime_session
        previous_kernel_plan = self._active_kernel_plan
        previous_llm = self._active_llm
        previous_addressed = self._active_addressed
        previous_chat_type = self._active_chat_type
        previous_agent_state = self.agent_state
        previous_continuity = self.continuity
        previous_turn_context = getattr(self, "_active_turn_context", "")
        self._active_actor_id = actor_id or "cli_owner"
        self._active_channel = source or "cli"
        self._active_addressed = bool(addressed)
        self._active_chat_type = chat_type or ""
        self._active_channel = source or "cli"
        self._active_turn_context = str(turn_context or "")[:8000]

        # Presenza: registra dove l'owner sta parlando ADESSO (mai per i
        # cicli autonomi) — i messaggi proattivi lo raggiungeranno lì.
        if self.presence is not None:
            try:
                self.presence.touch(source or "cli")
            except Exception:
                pass
        # Legame: se c'era un messaggio spontaneo recente, l'owner ha risposto
        if self.bonds is not None and (source or "cli") not in (
            "heartbeat", "cron", "subagent", "system",
        ):
            try:
                self.bonds.record_owner_reply()
            except Exception:
                pass

        # Privacy router: scegli il modello per questo turno
        session_type_for_privacy = resolve_session_type(
            source or "cli", sender or "user", self._active_chat_type,
        )
        requested_backend = str(llm_backend or "").strip().lower()
        if requested_backend == "auto":
            self._active_llm, route_info = self._automatic_llm(
                user_input, session_type_for_privacy,
            )
            self._last_llm_route = route_info
            privacy_reason = ""
            self.ui.status(
                f"[router economico: {self._active_llm.backend}/"
                f"{self._active_llm.model} — {route_info['reason']}]"
            )
        elif llm_backend or llm_model:
            self._active_llm = self._configured_llm(llm_backend, llm_model)
            self._last_llm_route = {
                "backend": self._active_llm.backend,
                "model": self._active_llm.model,
                "strategy": "explicit",
                "reason": "scelta manuale della chat",
            }
            privacy_reason = ""
            self.ui.status(
                f"[chat: {self._active_llm.backend}/{self._active_llm.model}]"
            )
        else:
            self._active_llm, privacy_reason = self._select_llm(
                session_type_for_privacy, user_input,
            )
            self._last_llm_route = {
                "backend": self._active_llm.backend,
                "model": self._active_llm.model,
                "strategy": "default",
                "reason": privacy_reason,
            }
        if privacy_reason:
            self.ui.status(
                f"[privacy: turno instradato su {self._active_llm.backend}/"
                f"{self._active_llm.model} — {privacy_reason}]"
            )
        max_iter = self.iteration_budget(
            source=source or "cli", backend=self._active_llm.backend,
        )
        route = self._build_route(
            source=source,
            sender=sender,
            actor_id=actor_id,
            chat_id=chat_id,
            thread_id=thread_id,
            session_key=session_key,
            parent_session_key=parent_session_key,
        )
        self._active_route = route
        self.agent_state = self._agent_state_for_route(route)
        self.continuity = ContinuityPromptBuilder(self.agent_state, self.journal)
        # Bus attività dopo il routing, così l'evento è filtrabile per chat.
        self._emit("user", text=str(user_input or "")[:4000], sender=sender or "user")
        runtime_session = self._get_runtime_session(route)
        self._active_runtime_session = runtime_session
        self._record_learning_signal(user_input, source, self._active_actor_id)
        tool_history_start = len(runtime_session.tool_history)
        journal_turn_id = self._start_journal_turn(user_input, source, route)
        journal_finished = False
        state_finished = False
        turn_token_start = runtime_session.tokens.total

        try:
            messages = self._get_session_messages(route)

            level = self.reasoner.classify(user_input)
            self.observer.log_thinking_level(level.value, user_input)
            active_goal = ""
            try:
                if self.agent_state.current and self.agent_state.current.active:
                    active_goal = self.agent_state.current.goal
            except Exception:
                active_goal = ""
            self._active_kernel_plan = self.kernel.prepare(
                user_input=user_input,
                thinking_level=level.value,
                tools_available=self.tools.names(),
                active_goal=active_goal,
                is_addressed=self._active_addressed,
            )
            selection = self.tool_router.select(
                user_input,
                source=source or "cli",
                mode=self._active_kernel_plan.mode,
                available=set(self.tools.names()),
            )
            self._active_tool_names = selection.names.intersection(self.tools.names())
            max_iter = self.iteration_budget(
                source=source or "cli",
                backend=self._active_llm.backend,
                mode=self._active_kernel_plan.mode,
            )
            # Il guardiano anti-loop conta per turno: un rifiuto di ieri non
            # deve impedire un tentativo legittimo oggi.
            self._failed_calls = {}
            self.agent_state.begin_turn(
                goal=self._active_kernel_plan.goal,
                source=source or "cli",
                actor=self._active_actor_id,
                session_key=route.session_key,
                thinking_level=level.value,
                plan=self._active_kernel_plan.steps,
            )

            enhanced_input = soften_reasoning(user_input, level.value)

            # In un GRUPPO più persone scrivono nella STESSA sessione: senza
            # etichettare chi parla, il modello vede un flusso indistinto di
            # "user" e non sa CON CHI parla (per lui sono tutti la stessa voce).
            # Anteponiamo "[Nome]:" così la cronologia distingue gli
            # interlocutori — combacia con quanto spiega describe_venue.
            llm_input = enhanced_input
            if self._active_chat_type in ("group", "supergroup", "channel") and sender:
                llm_input = f"[{sender}]: {enhanced_input}"

            self._build_system_prompt(user_input, source, sender)
            if messages and messages[0].get("role") == "system":
                messages[0]["content"] = self.messages[0]["content"]
            elif self.messages and self.messages[0]["role"] == "system":
                messages.insert(0, dict(self.messages[0]))

            messages.append({"role": "user", "content": llm_input})
            runtime_session.add_message("user", user_input)

            messages = self._trim_messages(messages)
            self._set_session_messages(messages, route)
            self._sync_route_snapshot(route, messages, runtime_session, state="running")

            self.plugin_mgr.fire_chain("before_llm_call", messages)
            use_native = self._active_llm.supports_function_calling
            streaming_enabled = (
                _config_bool("STREAMING_ENABLED", True)
                and hasattr(self.ui, "stream_token")
                and (
                    use_native
                    or getattr(self._active_llm, "supports_text_streaming", False)
                )
            )

            _had_tool_calls = False
            kernel_interventions = 0
            for iteration in range(1, max_iter + 1):
                turn_limit = max(0, int(getattr(cfg, "TURN_TOKEN_BUDGET", 48000)))
                if (turn_limit and iteration > 1
                        and runtime_session.tokens.total - turn_token_start >= turn_limit):
                    self.ui.stop_spinner()
                    self.ui.openvurp_say(
                        "Mi fermo qui per rispettare il budget token del turno. "
                        "Ho conservato lo stato: puoi chiedermi di continuare."
                    )
                    self.agent_state.fail(
                        "Per-turn token budget reached.", phase=AgentPhase.BLOCKED,
                    )
                    state_finished = True
                    break
                # Budget giornaliero: protegge dai loop che bruciano credito
                if self.budget is not None and self.budget.over_budget():
                    self.ui.stop_spinner()
                    self.ui.openvurp_say(
                        f"Ho raggiunto il tetto giornaliero di chiamate LLM "
                        f"({self.budget.status()}). Mi fermo per oggi — "
                        f"alza DAILY_LLM_BUDGET in .env se serve."
                    )
                    self.agent_state.fail("Daily LLM budget reached.",
                                          phase=AgentPhase.BLOCKED)
                    state_finished = True
                    break

                stream_handler, stream_state = (None, None)
                provider_event_handler = None
                if streaming_enabled:
                    stream_handler, stream_state = self._make_stream_handler()
                if self._active_llm.backend == "codex":
                    provider_event_handler = self._make_codex_activity_handler()
                if self.budget is not None:
                    self.budget.record_call()
                if _had_tool_calls:
                    self.ui.start_spinner("Elaboro i risultati...")
                else:
                    self.ui.start_spinner("Thinking...")

                trimmed_messages = self._trim_messages(messages)
                if trimmed_messages is not messages:
                    messages = trimmed_messages
                    self._set_session_messages(messages, route)
                tool_transport = getattr(
                    self._active_llm, "supports_tool_transport", use_native,
                )
                tools_schema = self._get_tools_schema() if tool_transport else []
                budget = self.context_mgr.check_budget(messages, tools_schema)
                if budget["over_budget"]:
                    self.ui.stop_spinner()
                    self.ui.status("[compattazione contesto...]")
                    messages = self._trim_messages(messages)
                    self._set_session_messages(messages, route)
                    self.ui.start_spinner("Thinking...")

                try:
                    if use_native:
                        if stream_handler:
                            llm_resp, duration_ms, tok_in, tok_out = \
                                self._active_llm.call_with_tools_streamed_timed(
                                    messages, tools_schema, on_text=stream_handler)
                        else:
                            llm_resp, duration_ms, tok_in, tok_out = \
                                self._active_llm.call_with_tools_timed(messages, tools_schema)
                        tool_calls_native = llm_resp.tool_calls
                        chat_text = llm_resp.text
                        response_text = llm_resp.text
                    else:
                        if self._active_llm.backend == "codex":
                            response_text, duration_ms, tok_in, tok_out = \
                                self._active_llm.call_streamed_with_timing(
                                    messages, on_text=stream_handler,
                                    on_event=provider_event_handler,
                                    tools_schema=tools_schema,
                                    on_tool=lambda name, args: self._execute_codex_tool(
                                        name, args, source,
                                    ))
                        else:
                            response_text, duration_ms, tok_in, tok_out = \
                                self._active_llm.call_with_timing(messages)
                        tool_calls_native = None
                        chat_text = None

                    self.observer.log_llm_call(
                        len(messages), len(response_text or ""),
                        duration_ms, tok_in, tok_out
                    )
                    runtime_session.tokens.add_call(tok_in, tok_out)
                    self._warn_if_degraded()

                except Exception as e:
                    self.ui.stop_spinner()

                    if self.context_mgr.is_context_overflow_error(e):
                        self.observer.log_event("context_overflow_detected", {
                            "error": str(e)[:200]
                        })
                        self.ui.status("[context overflow — compattazione...]")
                        messages = self._do_llm_compaction(messages)
                        self._set_session_messages(messages, route)
                        try:
                            self.ui.start_spinner("Thinking...")
                            if use_native:
                                tools_schema = self._get_tools_schema()
                                if streaming_enabled:
                                    stream_handler, stream_state = self._make_stream_handler()
                                    llm_resp, duration_ms, tok_in, tok_out = \
                                        self._active_llm.call_with_tools_streamed_timed(
                                            messages, tools_schema, on_text=stream_handler)
                                else:
                                    llm_resp, duration_ms, tok_in, tok_out = \
                                        self._active_llm.call_with_tools_timed(messages, tools_schema)
                                tool_calls_native = llm_resp.tool_calls
                                chat_text = llm_resp.text
                                response_text = llm_resp.text
                            else:
                                if self._active_llm.backend == "codex":
                                    stream_handler, stream_state = (None, None)
                                    if streaming_enabled:
                                        stream_handler, stream_state = self._make_stream_handler()
                                    provider_event_handler = self._make_codex_activity_handler()
                                    tools_schema = self._get_tools_schema()
                                    response_text, duration_ms, tok_in, tok_out = \
                                        self._active_llm.call_streamed_with_timing(
                                            messages, on_text=stream_handler,
                                            on_event=provider_event_handler,
                                            tools_schema=tools_schema,
                                            on_tool=lambda name, args: self._execute_codex_tool(
                                                name, args, source,
                                            ))
                                else:
                                    response_text, duration_ms, tok_in, tok_out = \
                                        self._active_llm.call_with_timing(messages)
                                tool_calls_native = None
                                chat_text = None
                            runtime_session.tokens.add_call(tok_in, tok_out)
                        except Exception as e2:
                            self.ui.stop_spinner()
                            self.ui.error(f"LLM fallita dopo compaction: {e2}")
                            self.agent_state.fail(
                                f"LLM failed after compaction: {e2}",
                                phase=AgentPhase.BLOCKED,
                            )
                            state_finished = True
                            return
                        finally:
                            self.ui.stop_spinner()
                    else:
                        self.observer.log_error(str(e), "llm_call")
                        from core.llm import LLMError
                        if isinstance(e, LLMError) and e.retryable:
                            # Backend giù (es. Ollama spento): pungola la
                            # sentinella così rileva subito la caduta, avvisa
                            # l'owner e segnala quando torna.
                            self.ui.error(
                                f"Il backend LLM non risponde: {e} "
                                f"— la sentinella lo tiene d'occhio e avvisa quando torna."
                            )
                            sentinel = getattr(self, "sentinel", None)
                            if sentinel is not None:
                                try:
                                    sentinel.check_now()
                                except Exception:
                                    pass
                        else:
                            self.ui.error(f"Connessione LLM fallita: {e}")
                        self.agent_state.fail(f"LLM call failed: {e}", phase=AgentPhase.BLOCKED)
                        state_finished = True
                        return
                finally:
                    self.ui.stop_spinner()

                tool_calls, chat_text, native_mode = self._resolve_tool_calls(
                    response_text=response_text or "",
                    chat_text=chat_text,
                    tool_calls_native=tool_calls_native,
                    use_native=use_native,
                )

                self.plugin_mgr.fire("after_llm_response", response=response_text or chat_text)

                streamed_live = bool(stream_state and stream_state.get("started"))
                if streamed_live:
                    self.ui.end_response()
                    self._emit("assistant_end")

                chat_directive = parse_response_directive(chat_text)
                if chat_directive.kind == "text" and not streamed_live:
                    if not tool_calls:
                        self.ui.start_response()
                        self.ui.stream_text(chat_directive.text)
                        self.ui.end_response()
                        self._emit("assistant_start")
                        self._emit("token", text=chat_directive.text)
                        self._emit("assistant_end")
                    elif chat_directive.text.strip():
                        self.ui.status(f"[{chat_directive.text.strip()[:120]}]")

                if not tool_calls:
                    final_directive = parse_response_directive(response_text or chat_text or "")
                    if final_directive.kind != "text":
                        self.agent_state.finish("", waiting_user=False)
                        state_finished = True
                        self._set_session_messages(messages, route)
                        self._sync_route_snapshot(route, messages, runtime_session, state="idle")
                        break

                    waiting_user = self._state_waiting_for_user(final_directive.text)
                    gate = self.kernel.review_final(
                        self._active_kernel_plan,
                        final_directive.text,
                        runtime_session.tool_history[tool_history_start:],
                        waiting_user=waiting_user,
                        interventions=kernel_interventions,
                    )
                    # All'ultimo giro la revisione non ha piu' un giro dopo in
                    # cui avvenire: insistere significherebbe buttare via una
                    # risposta gia' pronta e chiudere il turno a mani vuote.
                    if not gate.allowed and iteration >= max_iter:
                        self.ui.status("[revisione saltata: ultimo giro disponibile]")
                    elif not gate.allowed:
                        kernel_interventions += 1
                        self.agent_state.transition(AgentPhase.REVISING, gate.reason)
                        messages.append({"role": "user", "content": gate.prompt})
                        self._set_session_messages(messages, route)
                        self._sync_route_snapshot(route, messages, runtime_session, state="running")
                        continue

                    messages.append({"role": "assistant", "content": final_directive.text})
                    runtime_session.add_message("assistant", final_directive.text)
                    self.agent_state.transition(AgentPhase.REFLECTING, "Final response ready.")
                    self.agent_state.finish(
                        final_directive.text,
                        waiting_user=waiting_user,
                    )
                    self._sync_kernel_open_loop(
                        self._active_kernel_plan,
                        final_directive.text,
                        waiting_user,
                        source,
                    )
                    state_finished = True
                    self._finish_journal_turn(
                        journal_turn_id,
                        user_input,
                        final_directive.text,
                        runtime_session,
                        tool_history_start,
                        source,
                        route,
                        status="waiting_user" if waiting_user else "completed",
                    )
                    journal_finished = True
                    self._set_session_messages(messages, route)
                    self._sync_route_snapshot(route, messages, runtime_session, state="idle")
                    break

                if native_mode:
                    messages.append({
                        "role": "assistant",
                        "content": chat_text or "",
                        "tool_calls": [{"id": tc_id, "name": name, "args": args}
                                       for name, args, tc_id in tool_calls]
                    })
                else:
                    messages.append({"role": "assistant", "content": response_text})

                results = []
                batch_history_start = len(runtime_session.tool_history)
                tool_names = [tn for tn, _, _ in tool_calls if tn]
                self.agent_state.mark_execution(tool_names, iteration=iteration)

                runnable = [(tn, ta, tc_id) for tn, ta, tc_id in tool_calls
                            if tn or ta]

                # Batch di soli tool read-only: esecuzione in parallelo.
                parallel = (
                    len(runnable) > 1
                    and all(tn in self.PARALLEL_SAFE_TOOLS for tn, _, _ in runnable)
                )
                if parallel:
                    from concurrent.futures import ThreadPoolExecutor
                    with ThreadPoolExecutor(max_workers=min(4, len(runnable))) as pool:
                        outputs = list(pool.map(
                            lambda call: self._execute_tool(call[0], call[1], source),
                            runnable,
                        ))
                else:
                    outputs = [self._execute_tool(tn, ta, source)
                               for tn, ta, _ in runnable]

                from core.security.untrusted import is_untrusted_tool, wrap_untrusted
                for (tool_name, tool_args, tc_id), output in zip(runnable, outputs):
                    results.append(output or "")
                    truncated = truncate_tool_result(output, self.context_mgr.max_tokens)
                    # Avvolgi i contenuti esterni: dato da analizzare, non ordini
                    if is_untrusted_tool(tool_name):
                        truncated = wrap_untrusted(tool_name, truncated)

                    if native_mode:
                        messages.append({
                            "role": "tool_result",
                            "tool_call_id": tc_id,
                            "name": tool_name,
                            "content": truncated,
                        })
                    elif tool_name == "shell":
                        results[-1] = f"$ {tool_args.get('command', '')}\n{truncated}"
                    else:
                        results[-1] = f"[{tool_name}] {truncated}"

                if not native_mode and results:
                    output_block = "\n\n".join(results)
                    messages.append({
                        "role": "user",
                        "content": f"Output dei comandi:\n\n{output_block}"
                    })

                new_tool_history = runtime_session.tool_history[batch_history_start:]
                self.agent_state.record_observation(new_tool_history, outputs=results)

                self._set_session_messages(messages, route)
                self._sync_route_snapshot(route, messages, runtime_session, state="running")

                _had_tool_calls = True
                if len(tool_names) == 1:
                    self.ui.status(f"[{tool_names[0]} completato — elaboro...]")
                elif len(tool_names) > 1:
                    self.ui.status(
                        f"[{len(tool_names)} tool completati: {', '.join(tool_names)} — elaboro...]"
                    )

                needs_refresh = False
                for tool_name, tool_args, _ in tool_calls:
                    if self._touches_memory(tool_name, tool_args):
                        needs_refresh = True
                        break
                    if tool_name == "evolve_self":
                        evolved_file = tool_args.get("file", "")
                        if evolved_file:
                            self.bootstrap.invalidate(normalize_workspace_filename(evolved_file))
                        needs_refresh = True
                        break
                    if tool_name in (
                        "reload_plugins", "request_restart", "doctor_fix",
                        "memory_consolidate", "learning_feedback",
                        "learning_promote", "learning_rollback",
                        "task_journal", "reflection_note", "open_loop",
                        "agent_state", "anima_update", "remember",
                        "project", "forge",
                    ):
                        needs_refresh = True
                        break
                # Rebuild solo quando memoria/workspace/plugin sono cambiati:
                # ricostruire il prompt a ogni iterazione costa caro e
                # invalida il prompt caching del backend.
                if needs_refresh:
                    self._build_system_prompt(user_input, source, sender)
                    if messages and messages[0].get("role") == "system":
                        messages[0]["content"] = self.messages[0]["content"]
            else:
                self.agent_state.fail(
                    "Maximum tool/LLM iterations reached; waiting for user decision.",
                    phase=AgentPhase.BLOCKED,
                )
                state_finished = True
                # Dire solo "limite raggiunto" scarica sull'utente una decisione
                # che non ha gli elementi per prendere: non sa quante iterazioni
                # ha il turno, ne' cosa e' stato fatto.
                done = len(runtime_session.tool_history) - tool_history_start
                self.ui.openvurp_say(
                    f"Mi sono fermato dopo {max_iter} passaggi "
                    f"({done} azioni eseguite) senza chiudere la risposta. "
                    f"Dimmi 'continua' per riprendere da qui, oppure alza "
                    f"{'CLI_AGENT_MAX_ITERATIONS' if self._active_llm.backend in {'codex', 'claude_cli'} else 'MAX_ITERATIONS'} "
                    f"nel .env se succede spesso."
                )
        finally:
            if not state_finished:
                self.agent_state.fail(
                    "Turn ended before the autonomy loop reached a final state.",
                    phase=AgentPhase.INTERRUPTED,
                )
            if journal_turn_id and not journal_finished:
                self._finish_journal_turn(
                    journal_turn_id,
                    user_input,
                    "",
                    runtime_session,
                    tool_history_start,
                    source,
                    route,
                    status="interrupted",
                )
            self._active_actor_id = previous_actor
            self._active_channel = previous_channel
            try:
                self._sync_route_snapshot(route, self._get_session_messages(route), runtime_session, state="idle")
            except Exception:
                pass
            self._active_route = previous_route
            self._active_runtime_session = previous_runtime_session
            self._active_kernel_plan = previous_kernel_plan
            self._active_llm = previous_llm
            self._active_addressed = previous_addressed
            self._active_chat_type = previous_chat_type
            self.agent_state = previous_agent_state
            self.continuity = previous_continuity
            self._active_turn_context = previous_turn_context

    def _make_stream_handler(self):
        """Crea un callback per lo streaming live del testo verso la UI.

        Bufferizza l'inizio della risposta per non mostrare le direttive
        runtime ([[silence]], [[react:...]]) che vanno gestite a fine turno.
        Returns (handler, state) — state["started"] dice se il testo è già
        stato mostrato live.
        """
        state = {"started": False, "suppress": False, "buffer": ""}

        def handler(delta: str):
            if not delta or state["suppress"]:
                return
            if state["started"]:
                self.ui.stream_token(delta)
                self._emit("token", text=delta)
                return
            state["buffer"] += delta
            stripped = state["buffer"].lstrip()
            if not stripped:
                return
            if stripped.startswith("[["):
                state["suppress"] = True
                return
            if stripped[0] == "[" and len(stripped) < 2:
                return  # aspetta di capire se è una direttiva
            self.ui.stop_spinner()
            self.ui.start_response()
            self.ui.stream_token(state["buffer"])
            self._emit("assistant_start")
            self._emit("token", text=state["buffer"])
            state["started"] = True
            state["buffer"] = ""

        return handler, state

    def _make_codex_activity_handler(self):
        """Traduce gli item Codex App Server nell'attività UI di openvurp.

        Il testo finale resta sul canale token; ricerche, comandi, MCP e
        modifiche file diventano invece passaggi separati, visibili sia nella
        CLI/TUI sia nel pannello attività della dashboard.
        """
        shown_items: set[str] = set()

        def _preview(value, limit: int = 180) -> str:
            if isinstance(value, (list, tuple)):
                value = " ".join(str(part) for part in value)
            elif isinstance(value, dict):
                try:
                    value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
                except Exception:
                    value = str(value)
            clean = " ".join(str(value or "").split())
            return clean if len(clean) <= limit else clean[:limit - 1] + "…"

        def _show_tool(name: str, args: dict, step: str, text: str):
            self.ui.stop_spinner()
            show_tool = getattr(self.ui, "show_tool", None)
            if show_tool:
                show_tool(name, args)
            else:
                self.ui.status(f"[{name}: {text}]")
            self._emit("step", step=step, text=text or name)

        def handler(event: dict):
            method = str(event.get("method", "") or "")
            params = event.get("params") or {}
            item = params.get("item") or {}
            item_type = str(item.get("type", "") or "")
            item_id = str(item.get("id", "") or "")

            if method == "item/started" and item_id not in shown_items:
                shown_items.add(item_id)
                if item_type == "commandExecution":
                    command = _preview(item.get("command")) or "comando"
                    self.ui.stop_spinner()
                    show_cmd = getattr(self.ui, "show_cmd", None)
                    if show_cmd:
                        show_cmd(command)
                    else:
                        self.ui.status(f"[shell: {command}]")
                    self._emit("step", step="shell", text=command)
                elif item_type == "webSearch":
                    action = item.get("action") or {}
                    detail = (
                        item.get("query") or action.get("query")
                        or action.get("queries") or action.get("url")
                        or action.get("pattern") or "ricerca web"
                    )
                    detail = _preview(detail)
                    _show_tool("web_search", {"query": detail}, "web", detail)
                elif item_type == "mcpToolCall":
                    server = _preview(item.get("server"), 60)
                    tool = _preview(item.get("tool"), 80)
                    label = f"mcp_{server}_{tool}" if server else f"mcp_{tool}"
                    detail = f"{server}/{tool}".strip("/")
                    _show_tool(
                        label, {"name": detail, "arguments": item.get("arguments")},
                        "mcp", detail,
                    )
                elif item_type == "fileChange":
                    changes = item.get("changes") or []
                    paths = [_preview(change.get("path"), 100) for change in changes
                             if isinstance(change, dict) and change.get("path")]
                    detail = ", ".join(paths[:4]) or "modifica file"
                    _show_tool("file_change", {"path": detail}, "file", detail)
                elif item_type == "imageView":
                    detail = _preview(item.get("path")) or "immagine"
                    _show_tool("image_view", {"path": detail}, "image", detail)
                elif item_type == "collabToolCall":
                    detail = _preview(item.get("tool")) or "collaborazione agente"
                    _show_tool("agent_collaboration", {"name": detail}, "agent", detail)
                elif item_type == "contextCompaction":
                    self.ui.stop_spinner()
                    self.ui.status("[Codex compatta il contesto]")
                    self._emit("step", step="context", text="compattazione contesto")

            if method == "item/completed":
                if item_type == "agentMessage" and item.get("phase") == "commentary":
                    commentary = _preview(item.get("text"), 260)
                    if commentary:
                        self.ui.stop_spinner()
                        self.ui.status(f"[Codex: {commentary}]")
                        self._emit("step", step="thinking", text=commentary)
                elif item_type == "commandExecution":
                    output = str(item.get("aggregatedOutput", "") or "").strip()
                    if output:
                        show_output = getattr(self.ui, "show_output", None)
                        if show_output:
                            show_output(
                                output,
                                is_error=(item.get("status") == "failed"),
                            )
                elif item_type == "mcpToolCall":
                    output = item.get("error") or item.get("result")
                    if output:
                        show_output = getattr(self.ui, "show_output", None)
                        if show_output:
                            show_output(
                                _preview(output, 700),
                                is_error=bool(item.get("error")),
                            )

        return handler

    def _warn_if_degraded(self) -> None:
        """Segnala una sola volta che il backend gira in modalità ridotta.

        Un degrado silenzioso è la cosa peggiore: l'agente sembra semplicemente
        diventato stupido, senza che nulla lo spieghi.
        """
        reason = str(getattr(self._active_llm, "degraded_reason", "") or "")
        if not reason:
            return
        if reason == getattr(self, "_last_degraded_warning", ""):
            return
        self._last_degraded_warning = reason
        self.ui.status(f"[backend in modalità ridotta] {reason}")
        self.observer.log_event("llm_backend_degraded", {"reason": reason[:300]})

    def _execute_codex_tool(
        self, tool_name: str, tool_args: dict, source: str = "cli",
    ) -> str:
        """Esegue un tool openvurp e limita solo il payload rimandato a Codex.

        L'esecuzione, le approvazioni e l'audit restano quelli di openvurp. Il
        risultato completo non viene perso dal runtime, ma una pagina web o un
        comando molto verboso non puo' occupare da solo l'intero contesto LLM.
        """
        output = str(self._execute_tool(tool_name, tool_args, source) or "")
        from core.security.untrusted import is_untrusted_tool, wrap_untrusted
        if is_untrusted_tool(tool_name):
            output = wrap_untrusted(tool_name, output)
        try:
            import config as cfg
            limit = int(getattr(cfg, "CODEX_TOOL_RESULT_MAX_CHARS", 8000) or 8000)
        except (TypeError, ValueError, ImportError):
            limit = 8000
        limit = max(1000, min(limit, 50000))
        if len(output) <= limit:
            return output

        marker = "\n\n[... risultato tool compattato per ridurre i token ...]\n\n"
        available = max(1, limit - len(marker))
        head_size = int(available * 0.65)
        tail_size = available - head_size
        return output[:head_size] + marker + output[-tail_size:]

    @staticmethod
    def iteration_budget(source: str = "cli", backend: str = "",
                         mode: str = "") -> int:
        """Quanti giri di autonomia ha questo turno.

        Estratto dal loop perche' e' una regola con conseguenze visibili: se il
        budget non lascia spazio a un secondo passaggio, ogni turno che richiede
        una revisione o un tool testuale finisce con "limite di iterazioni"
        senza aver concluso nulla.
        """
        import config as cfg

        budget = int(getattr(cfg, "MAX_ITERATIONS", 20) or 20)
        if (source or "") == "heartbeat":
            budget = min(budget, int(getattr(cfg, "HEARTBEAT_MAX_ITERATIONS", 8)))
        if backend in {"codex", "claude_cli"}:
            budget = min(budget, max(2, int(getattr(cfg, "CLI_AGENT_MAX_ITERATIONS", 6))))
        if mode in {"chat", "answer"}:
            budget = min(budget, int(getattr(cfg, "CHAT_MAX_ITERATIONS", 4)))
        # Sotto i due giri il loop non puo' nemmeno rileggere il risultato di
        # un tool: sarebbe un turno monco per costruzione.
        return max(2, budget)

    @staticmethod
    def _call_fingerprint(tool_name: str, tool_args: dict) -> str:
        """Identita' di una chiamata, per riconoscerne la ripetizione."""
        try:
            payload = json.dumps(tool_args or {}, sort_keys=True, ensure_ascii=False)
        except (TypeError, ValueError):
            payload = repr(tool_args)
        return f"{tool_name}:{payload}"[:600]

    def _loop_guard(self, tool_name: str, tool_args: dict) -> str:
        """Blocca la stessa chiamata gia' fallita identica troppe volte.

        Un modello davanti a un rifiuto stabile (budget esaurito, permesso
        negato) tende a ritentare identico: quattro `anima_update` di fila che
        rispondono "budget esaurito" non aggiungono nulla, bruciano le
        iterazioni e fanno scadere il turno del provider. Dopo N tentativi il
        runtime smette di eseguire e chiede al modello di fermarsi.
        """
        try:
            import config as cfg
            limit = int(getattr(cfg, "TOOL_MAX_IDENTICAL_FAILURES", 3) or 3)
        except Exception:
            limit = 3
        limit = max(2, limit)
        failures = getattr(self, "_failed_calls", None)
        if failures is None:
            failures = self._failed_calls = {}
        count = failures.get(self._call_fingerprint(tool_name, tool_args), 0)
        if count < limit:
            return ""
        self.ui.status(f"[{tool_name}: stessa chiamata fallita {count}x — mi fermo]")
        self.observer.log_event("tool_loop_blocked", {
            "tool": tool_name, "failures": count,
        })
        return (
            f"[BLOCCATO DAL RUNTIME] '{tool_name}' è già fallito {count} volte con "
            f"argomenti identici. Il risultato non cambierà ritentando. Smetti di "
            f"chiamarlo: spiega all'utente cosa hai provato e perché non è "
            f"possibile, oppure cambia strategia."
        )

    def _note_call_outcome(self, tool_name: str, tool_args: dict, success: bool) -> None:
        failures = getattr(self, "_failed_calls", None)
        if failures is None:
            failures = self._failed_calls = {}
        key = self._call_fingerprint(tool_name, tool_args)
        if success:
            failures.pop(key, None)
        else:
            failures[key] = failures.get(key, 0) + 1

    def _execute_tool(self, tool_name: str, tool_args: dict, source: str = "cli") -> str:
        """Esegue un singolo tool con safety check."""
        effective_tool_name = tool_name or "shell"
        mode = getattr(self, "approval_mode", "safe")
        self._current_tool_source = source

        blocked = self._loop_guard(effective_tool_name, tool_args)
        if blocked:
            return blocked

        # Plan mode: solo osservazione. Le azioni che modificano qualcosa
        # vengono restituite come passi del piano, non eseguite.
        if mode == "plan" and effective_tool_name not in self.PLAN_SAFE_TOOLS:
            self.ui.status(f"[plan mode: {effective_tool_name} non eseguito]")
            return (
                f"[PLAN MODE] Il tool '{effective_tool_name}' non viene eseguito in plan mode. "
                f"Registra questa azione come passo del piano (con argomenti precisi) "
                f"e presenta il piano completo all'utente. Eseguirai dopo l'approvazione, "
                f"quando l'utente passa a /mode safe o /mode auto."
            )

        # Patti: le promesse fatte all'owner valgono più di qualsiasi
        # modalità — il runtime le fa rispettare anche in auto mode.
        if self.pacts is not None:
            try:
                pact_ok, pact_reason, pact_confirm = self.pacts.check_tool_call(
                    effective_tool_name,
                    tool_args if isinstance(tool_args, dict) else {},
                    source=source,
                )
            except Exception:
                pact_ok, pact_reason, pact_confirm = True, "", False
            if not pact_ok:
                self.ui.status("[bloccato da un patto]")
                return pact_reason
            if pact_confirm:
                if not self.ui.confirm(
                    f"Patto attivo: {pact_reason}\n  Confermi {effective_tool_name}?"
                ):
                    self.ui.status("[bloccato dall'owner]")
                    return "[BLOCCATO] L'owner non ha confermato l'azione esterna (patto attivo)."

        # Egress guard: niente segreti in uscita, allowlist domini opzionale.
        # Vale anche in auto mode e nei cicli autonomi.
        try:
            from core.security.egress import check_egress
            egress_ok, egress_reason = check_egress(
                effective_tool_name,
                tool_args if isinstance(tool_args, dict) else {},
            )
        except Exception:
            egress_ok, egress_reason = True, ""
        if not egress_ok:
            self.ui.status("[egress bloccato]")
            return egress_reason

        # Auto mode: pre-approva azioni rischiose ma non critiche
        # (i comandi CRITICAL restano bloccati in ogni modalità).
        preapproved = mode == "auto"
        allowed, reason = self.rbac.check_tool(self._active_actor_id, effective_tool_name)
        if not allowed:
            self.ui.status("[permesso negato]")
            return f"[PERMESSO NEGATO] {reason}"

        # MCP tool
        if effective_tool_name.startswith("mcp_") and self._mcp_client:
            parts = effective_tool_name.split("_", 2)
            if len(parts) >= 3:
                server_name = parts[1]
                mcp_tool_name = parts[2]
                self.ui.status(f"[mcp: {server_name}/{mcp_tool_name}]")
                return self._mcp_client.call_tool(server_name, mcp_tool_name, tool_args)

        # Shell tool
        if tool_name == "shell" or tool_name == "":
            command = tool_args.get("command", "") if isinstance(tool_args, dict) else str(tool_args)
            if not command.strip():
                return ""
            shell_args = dict(tool_args) if isinstance(tool_args, dict) else {"command": command}
            shell_args["command"] = command

            risk = self.safety.classify(command)

            if risk == ActionRisk.CRITICAL:
                self.ui.error(f"Comando bloccato (critico): {command[:80]}")
                return f"[BLOCCATO — comando critico]"

            if self.safety.is_critical_file(command):
                if mode == "auto":
                    preapproved = True
                elif not self.ui.confirm(f"Modifica file critico:\n  {command[:100]}"):
                    self.ui.status("[bloccato]")
                    return f"[BLOCCATO dall'utente]"
                else:
                    preapproved = True

            self.ui.show_cmd(command)
            self._emit("step", step="shell", text=command[:160])
            result = self.executor.execute(
                "shell",
                shell_args,
                ui=self.ui,
                preapproved=preapproved,
                actor=self._active_actor_id,
                source=source,
            )
            self.ui.show_output(result.output, is_error=not result.success)
            self._record_tool_learning(result, "shell", shell_args, source)
            self._active_runtime_session.add_tool_result(result, "shell", shell_args)
            return self._tool_result_text(result)

        # Background terminal sessions
        if tool_name == "process_start":
            command = tool_args.get("command", "") if isinstance(tool_args, dict) else ""
            if not command.strip():
                return ""

            risk = self.safety.classify(command)
            if risk == ActionRisk.CRITICAL:
                self.ui.error(f"Comando bloccato (critico): {command[:80]}")
                return "[BLOCCATO — comando critico]"

            self.ui.show_cmd(command)

        if tool_name == "process_write":
            text = tool_args.get("text", "") if isinstance(tool_args, dict) else ""
            if text.strip():
                risk = self.safety.classify(text)
                if risk == ActionRisk.CRITICAL:
                    self.ui.error(f"Input bloccato (critico): {text[:80]}")
                    return "[BLOCCATO — input critico]"

        # Structured tool
        # Plugin hook
        hook_result = self.plugin_mgr.fire("before_tool_call",
                                           tool_name=tool_name, args=tool_args)
        for plugin_id, result in hook_result.items():
            if result is None:
                self.ui.status(f"[plugin {plugin_id} ha bloccato {tool_name}]")
                return "[BLOCCATO da plugin]"

        show_tool = getattr(self.ui, "show_tool", None)
        if show_tool:
            show_tool(tool_name, tool_args if isinstance(tool_args, dict) else None)
        else:
            self.ui.status(f"[tool: {tool_name}]")
        preview = ""
        if isinstance(tool_args, dict):
            for key in (
                "query", "url", "path", "file", "command", "pattern",
                "name", "task", "action",
            ):
                if tool_args.get(key):
                    preview = " ".join(str(tool_args[key]).split())[:140]
                    break
        event_text = f"{tool_name} — {preview}" if preview else tool_name
        self._emit("step", step="tool", text=event_text)
        result = self.executor.execute(
            tool_name,
            tool_args,
            ui=self.ui,
            preapproved=preapproved,
            actor=self._active_actor_id,
            source=source,
        )

        if result.success:
            self.ui.show_output(result.output)
        else:
            self.ui.show_output(result.error or result.output, is_error=True)
        self._note_call_outcome(tool_name, tool_args, result.success)
        self._record_tool_learning(result, tool_name, tool_args, source)

        self._active_runtime_session.add_tool_result(result, tool_name, tool_args)

        # Plugin hook
        self.plugin_mgr.fire("after_tool_call", tool_name=tool_name, result=result)

        return self._tool_result_text(result)

    def _emit(self, kind: str, **data) -> None:
        """Pubblica un'attività sul bus (dashboard live). Mai bloccante."""
        try:
            from core import activity
            route = getattr(self, "_active_route", None)
            meta = {
                "source": getattr(self, "_active_channel", "cli"),
                "session_key": getattr(route, "session_key", ""),
                "chat_id": getattr(route, "chat_id", ""),
                "actor_id": getattr(self, "_active_actor_id", ""),
            }
            meta.update(data)
            activity.publish(kind, **meta)
        except Exception:
            pass

    def _tool_result_text(self, result) -> str:
        """Testo del risultato tool da restituire al MODELLO.

        Su successo: l'output. Su fallimento NON restituire mai stringa vuota:
        il modello deve sapere cosa è andato storto, altrimenti crede che
        l'azione sia semplicemente "non andata" senza capire perché (es. un
        rifiuto dell'owner viene scambiato per un errore di esecuzione).
        """
        if result.success:
            return result.output
        detail = result.error or result.output or "azione non riuscita"
        # Rifiuto dell'owner: il messaggio è già autoesplicativo, niente "[FALLITO]"
        # (non è un fallimento, è una decisione).
        if getattr(result, "error_type", None) == ErrorType.PERMISSION:
            return detail
        if getattr(result, "output", "") and result.error:
            # Alcuni tool falliscono ma producono comunque output parziale utile.
            return f"[FALLITO] {result.error}\n{result.output}"
        return f"[FALLITO] {detail}"

    def _get_tools_schema(self) -> list[dict]:
        """Genera schema tool per il backend corrente."""
        if self._active_llm.backend in (
            "openai", "openai_compatible", "groq", "ollama", "codex",
        ):
            return self.tools.to_openai_schema(self._active_tool_names)
        elif self._active_llm.backend == "anthropic":
            return self.tools.to_anthropic_schema(self._active_tool_names)
        return []

    def _resolve_tool_calls(self, response_text: str, chat_text: str | None,
                            tool_calls_native: list[ToolCall] | None,
                            use_native: bool) -> tuple[list[tuple[str, dict, str]], str, bool]:
        """Normalizza tool calls da backend nativo o fallback regex.

        Returns:
            (tool_calls, chat_text, native_mode)
        """
        native_mode = use_native and self._active_llm.supports_function_calling
        chat_text = chat_text if chat_text is not None else response_text

        if native_mode and tool_calls_native is not None:
            tool_calls = [(tc.name, tc.args, tc.id) for tc in tool_calls_native]
            if tool_calls:
                return tool_calls, chat_text, True

            # Compat: se Ollama non emette tool_calls ma scrive ancora il vecchio formato,
            # prova a recuperare con il parser regex invece di chiudere il turno a vuoto.
            if self._active_llm.backend == "ollama":
                parsed_calls, parsed_text = self._parse_response(response_text or chat_text or "")
                if len(parsed_calls) > 1:
                    parsed_calls = parsed_calls[:1]
                tool_calls = [
                    (name, args, str(uuid.uuid4())[:8])
                    for name, args in parsed_calls
                ]
                return tool_calls, parsed_text, False

            return [], chat_text, True

        parsed_calls, parsed_text = self._parse_response(response_text or "")
        if len(parsed_calls) > 1:
            parsed_calls = parsed_calls[:1]
        tool_calls = [(name, args, str(uuid.uuid4())[:8])
                      for name, args in parsed_calls]
        return tool_calls, parsed_text, False

    def _parse_response(self, response: str) -> tuple[list[tuple[str, dict]], str]:
        """Parse response per estrarre tool calls e testo (fallback regex per Ollama).

        Cattura tutte le varianti che i modelli Ollama producono:
          ```TOOL:name\n{json}\n```        (formato standard)
          ```tool:name\n{json}\n```        (lowercase)
          ```name\n{json}\n```             (solo nome tool, senza TOOL:)
          TOOL:name\n{json}                (senza backtick)
          tool: name\n{json}               (con spazio)
          ```SHELL\ncomando\n```           (shell)
          ```shell\ncomando\n```           (shell lowercase)
          ```bash\ncomando\n```            (bash)
        """
        tool_calls = []
        chat_parts = []
        known_tools = set(self.tools.names())

        # Fase 1: Pattern con backtick — ```...``` (anche senza ``` di chiusura)
        # Cattura: ```TOOL:name, ```tool:name, ```name, ```SHELL, ```shell, ```bash
        backtick_pattern = re.compile(
            r'```\s*(?:(?:[Tt][Oo][Oo][Ll]\s*:\s*)?(\w+))\s*\n(.*?)(?:```|$)',
            re.DOTALL,
        )

        last_end = 0
        for match in backtick_pattern.finditer(response):
            before = response[last_end:match.start()].strip()
            if before:
                chat_parts.append(before)

            raw_name = match.group(1).strip()
            content = match.group(2).strip()
            name_lower = raw_name.lower()

            if name_lower in ("shell", "bash", "sh"):
                tool_calls.append(("shell", {"command": content}))
            elif raw_name in known_tools:
                args = self._parse_tool_json(content, raw_name)
                tool_calls.append((raw_name, args))
            elif name_lower in {t.lower() for t in known_tools}:
                # Match case-insensitive
                real_name = next(t for t in known_tools if t.lower() == name_lower)
                args = self._parse_tool_json(content, real_name)
                tool_calls.append((real_name, args))
            else:
                # Non è un tool noto — è un blocco codice normale, rimettilo come testo
                chat_parts.append(response[match.start():match.end()])

            last_end = match.end()

        after = response[last_end:].strip()
        if after:
            chat_parts.append(after)

        # Se abbiamo trovato tool call con backtick, ritorna
        if tool_calls:
            chat_text = "\n".join(p for p in chat_parts if p)
            return tool_calls, chat_text

        # Fase 2: Pattern senza backtick — TOOL:name\n{json} o tool: name\n{json}
        # Anche: nome_tool\n{json} se il nome è un tool noto
        chat_parts = []
        plain_pattern = re.compile(
            r'(?:^|\n)\s*(?:[Tt][Oo][Oo][Ll]\s*:\s*)?(\w+)\s*\n\s*(\{[^}]*(?:\{[^}]*\}[^}]*)?\})',
            re.MULTILINE,
        )

        last_end = 0
        for match in plain_pattern.finditer(response):
            raw_name = match.group(1).strip()
            content = match.group(2).strip()

            # Verifica che sia un tool noto (altrimenti ignora)
            real_name = None
            if raw_name in known_tools:
                real_name = raw_name
            else:
                name_lower = raw_name.lower()
                for t in known_tools:
                    if t.lower() == name_lower:
                        real_name = t
                        break

            if not real_name:
                continue

            before = response[last_end:match.start()].strip()
            if before:
                chat_parts.append(before)

            args = self._parse_tool_json(content, real_name)
            tool_calls.append((real_name, args))
            last_end = match.end()

        if tool_calls:
            after = response[last_end:].strip()
            if after:
                chat_parts.append(after)
            chat_text = "\n".join(p for p in chat_parts if p)
            return tool_calls, chat_text

        # Fase 3: Nessuna tool call trovata — tutto è testo
        return [], response.strip()

    def _parse_tool_json(self, content: str, tool_name: str) -> dict:
        """Parse JSON robusto per tool args."""
        # 1. Parse diretto
        try:
            args = json.loads(content)
            if isinstance(args, dict):
                return args
        except json.JSONDecodeError:
            pass

        # 2. Fix trailing comma
        fixed = re.sub(r',\s*}', '}', content)
        fixed = re.sub(r',\s*]', ']', fixed)
        try:
            args = json.loads(fixed)
            if isinstance(args, dict):
                return args
        except json.JSONDecodeError:
            pass

        # 3. Aggiungi } mancanti
        open_braces = content.count('{') - content.count('}')
        if open_braces > 0:
            fixed = content + '}' * open_braces
            fixed = re.sub(r',\s*}', '}', fixed)
            try:
                args = json.loads(fixed)
                if isinstance(args, dict):
                    return args
            except json.JSONDecodeError:
                pass

        # 4. Estrai key:value manualmente
        pairs = {}
        kv_pattern = re.compile(r'"(\w+)"\s*:\s*("(?:[^"\\]|\\.)*"|\d+(?:\.\d+)?|true|false|null|\{[^}]*\}|\[[^\]]*\])')
        for m in kv_pattern.finditer(content):
            key = m.group(1)
            val = m.group(2)
            try:
                pairs[key] = json.loads(val)
            except json.JSONDecodeError:
                pairs[key] = val.strip('"')

        if pairs:
            return pairs

        # 5. Fallback
        return {"input": content}

    def _sync_kernel_open_loop(
        self,
        plan: KernelPlan | None,
        final_text: str,
        waiting_user: bool,
        source: str,
    ) -> None:
        """Keep durable follow-ups aligned with runtime-gated tasks."""
        if not plan:
            return
        try:
            task = self.agent_state.current
            stable_goal_id = uuid.uuid5(uuid.NAMESPACE_URL, plan.goal or "").hex[:12]
            tag = f"task:{task.task_id}" if task else f"goal:{stable_goal_id}"
            loops = self.journal.list_open_loops(include_closed=True)
            matching_open = [
                loop for loop in loops
                if loop.status == "open" and tag in (loop.tags or [])
            ]

            if self.kernel.should_track_open_loop(plan, final_text, waiting_user):
                if matching_open:
                    return
                self.journal.add_open_loop(
                    title=f"Continue: {plan.goal[:120]}",
                    description=(
                        "Runtime kernel left this task open. "
                        f"Last response: {' '.join((final_text or '').split())[:500]}"
                    ),
                    source=source or "cli",
                    actor=self._active_actor_id,
                    tags=["kernel", plan.mode, tag],
                )
                return

            for loop in matching_open:
                self.journal.close_open_loop(
                    loop.id,
                    resolution=f"Task completed: {plan.goal[:500]}",
                    actor=self._active_actor_id,
                    source=source or "cli",
                )
        except Exception:
            pass

    def _touches_memory(self, tool_name: str, args: dict) -> bool:
        """Check se un'azione tocca la memoria o file workspace."""
        if tool_name == "shell":
            cmd = args.get("command", "")
            return MEMORY_DIR in cmd or "memory/" in cmd
        if tool_name in ("write_file", "edit_file", "edit_lines", "append_file"):
            path = args.get("path", "")
            if MEMORY_DIR in path or "memory/" in path:
                return True
            # Check se tocca un file workspace
            workspace_files = {"SOUL.md", "IDENTITY.md", "AGENTS.md", "USER.md",
                             "TOOLS.md", "MEMORY.md", "HEARTBEAT.md"}
            basename = normalize_workspace_filename(os.path.basename(path))
            if basename in workspace_files:
                return True
        if tool_name in (
            "learning_feedback", "learning_promote", "memory_consolidate",
            "task_journal", "reflection_note", "open_loop",
            "capability_lease",
            "agent_state",
        ):
            return True
        return False

    def _state_waiting_for_user(self, assistant_text: str) -> bool:
        text = (assistant_text or "").strip().lower()
        if not text:
            return False
        if "?" in text:
            return True
        return any(marker in text for marker in (
            "mi serve",
            "dimmi",
            "scegli",
            "confermi",
            "vuoi che",
            "serve una tua",
            "aspetto",
        ))

    def _start_journal_turn(self, user_input: str, source: str,
                            route: SessionRoute) -> str:
        try:
            return self.journal.start_turn(
                user_input=user_input,
                source=source or "cli",
                actor=self._active_actor_id,
                session_key=route.session_key,
            )
        except Exception:
            return ""

    def _finish_journal_turn(self, turn_id: str, user_input: str,
                             assistant_text: str, runtime_session: Session,
                             tool_history_start: int, source: str,
                             route: SessionRoute,
                             status: str = "completed") -> None:
        if not turn_id:
            return
        try:
            tool_history = runtime_session.tool_history[tool_history_start:]
            self.journal.finish_turn(
                turn_id=turn_id,
                user_input=user_input,
                assistant_text=assistant_text,
                tool_history=tool_history,
                status=status,
                source=source or "cli",
                actor=self._active_actor_id,
                session_key=route.session_key,
            )
            # Task riusciti con tool = materia prima per candidati "procedure"
            if status == "completed" and tool_history:
                tools_used = []
                for item in tool_history:
                    name = str(item.get("tool") or "")
                    if name and name not in tools_used:
                        tools_used.append(name)
                if tools_used and self._learning_enabled(source, self._active_actor_id):
                    self.learning.record_task_completion(
                        goal=user_input, tools_used=tools_used, source=source or "cli",
                    )
        except Exception:
            pass

    def _learning_enabled(self, source: str, actor_id: str) -> bool:
        return (source or "cli") == "cli" or (actor_id or "") == "cli_owner"

    def _record_learning_signal(self, user_input: str, source: str,
                                actor_id: str) -> None:
        # I prompt di sistema (heartbeat) non sono la voce dell'owner: se
        # entrassero qui, il testo del prompt finirebbe classificato come
        # "ricordo esplicito" e di notte consolidato in MEMORY.md come fatto.
        if (source or "") == "heartbeat":
            return
        if not self._learning_enabled(source, actor_id):
            return
        try:
            self.learning.record_user_signal(
                user_input,
                actor=actor_id or "user",
                source=source or "cli",
            )
        except Exception:
            pass

    def _record_tool_learning(self, result: ToolResult, tool_name: str,
                              args: dict, source: str) -> None:
        if result.success:
            return
        if not self._learning_enabled(source, self._active_actor_id):
            return
        try:
            self.learning.record_tool_failure(
                tool_name,
                args or {},
                result,
                actor=self._active_actor_id,
                source=source or "cli",
            )
        except Exception:
            pass

    # ── Public API ──

    def get_session_trace(self) -> str:
        return self.observer.format_trace()

    def save_session(self):
        self.session.save()
        self._sync_route_snapshot(SessionRoute.build(), self.messages, self.session, state="idle")
        for route_session in self._route_runtime_sessions.values():
            try:
                route_session.save()
            except Exception:
                pass
        self.observer.save_session_trace()
        # Plugin hook
        self.plugin_mgr.fire("on_session_end", session=self.session)

    def cleanup(self):
        """Cleanup risorse (MCP, cache, etc.)."""
        if self._mcp_client:
            self._mcp_client.disconnect_all()
        if self.llm._cache:
            self.llm._cache.cleanup()
