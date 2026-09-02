"""Selezione dei tool da esporre al modello in ogni turno.

I tool restano tutti registrati nell'Executor: qui si decide soltanto quali
schemi inviare al provider. Risparmiare token e' utile, ma un agente che non
vede un tool si comporta come se non lo avesse: la selezione aggressiva per
keyword lo rendeva improvvisamente incapace di fare cose che sapeva fare.

Per questo il default e' ``TOOL_ROUTER_MODE=off``: tutti i tool, sempre. Le
modalita' ``wide`` (pack essenziali + keyword) e ``strict`` (solo core +
keyword) restano disponibili per chi vuole comprimere i token di input.
"""

from __future__ import annotations

from dataclasses import dataclass


PACKS: dict[str, set[str]] = {
    "core": {
        "shell", "read_file", "grep", "find_files", "load_skill",
        "load_toolset", "agent_state", "doctor", "vurpub_search",
    },
    "files": {
        "write_file", "edit_file", "edit_lines", "append_file",
        "process_list", "process_sessions", "process_start", "process_read",
        "process_write", "process_stop", "process_kill",
    },
    "web": {
        "web_fetch", "web_search", "browser", "browser_setup",
        "browser_devtools", "desktop_screenshot", "image_analyze",
        "audio_transcribe", "pdf_read",
    },
    "memory": {
        "remember", "memory_consolidate", "learning_feedback",
        "learning_review", "learning_promote", "learning_rollback",
        "task_journal", "reflection_note", "open_loop",
        "pact",
    },
    "communication": {
        "notify", "notify_voice", "notify_file", "notify_photo",
        "notify_poll", "schedule_notify", "list_schedule", "cancel_schedule",
    },
    "runtime": {
        "doctor_fix", "capability_lease", "request_restart", "read_self",
        "evolve_self", "list_plugins", "reload_plugins",
        "scaffold_plugin", "forge",
    },
    "agents": {
        "second_opinion", "subagent_spawn", "subagent_list", "subagent_wait",
        "subagent_wait_all", "subagent_kill",
        "swarm_spawn", "swarm_ask", "swarm_discuss", "swarm_list",
        "swarm_dismiss", "swarm_transcript",
    },
    "voice": {
        "speak", "listen_mic", "list_voices",
    },
    "security": {
        "audit", "integrity", "sentinel", "setup_marker", "viewport",
    },
    "marketplace": {
        "vurpub_search", "vurpub_pull", "vurpub_candidates",
        "vurpub_approve", "vurpub_reject", "vurpub_share",
    },
}

KEYWORDS: dict[str, tuple[str, ...]] = {
    "files": (
        "codice", "code", "file", "progetto", "repository", "repo", "test",
        "bug", "fix", "implement", "modific", "scriv", "crea", "build",
        "refactor", "terminale", "shell", "processo", "server",
    ),
    "web": (
        "web", "internet", "sito", "pagina", "url", "browser", "cerca online",
        "cerca sul web", "cerca su internet", "online", "notizie", "aggiornato",
        "ricerca", "download", "pdf", "immagine", "foto", "audio", "screenshot",
    ),
    "memory": (
        "memoria", "ricorda", "lezione", "riflessione", "patto",
    ),
    "communication": (
        "telegram", "notifica", "invia", "messaggio", "sondaggio", "poll",
        "programma", "schedule", "promemoria", "voce",
    ),
    "runtime": (
        "plugin", "runtime", "riavvia", "restart", "diagnosi", "doctor",
        "capacita", "evolv", "bootstrap", "forge",
    ),
    "agents": (
        "subagent", "sub-agent", "delega", "parallelo", "second opinion",
        "swarm", "sciame", "specialista", "esperto", "team", "squadra",
        "confrontati", "chiedi a", "dubbio", "pareri",
    ),
    "voice": (
        "voce", "parla", "ascolta", "microfono", "audio", "leggi ad alta voce",
    ),
    "security": (
        "audit", "integrita", "integrity", "sentinella", "sentinel", "sicurezza",
    ),
    "marketplace": (
        "vurpub", "bancone", "skill condivisa", "soluzione condivisa",
    ),
}


# In modalita' "wide" questi pack sono sempre attivi: sono le capacita' che
# l'agente usa in conversazione normale senza che l'utente le nomini
# ("ricordati che...", "guarda se...", "avvisami quando...").
ALWAYS_ON_PACKS = ("core", "files", "memory", "communication", "web")


def _router_mode() -> str:
    try:
        import config as cfg
        mode = str(getattr(cfg, "TOOL_ROUTER_MODE", "off") or "off")
    except Exception:
        mode = "off"
    mode = mode.strip().lower()
    return mode if mode in {"off", "wide", "strict"} else "off"


@dataclass
class ToolSelection:
    names: set[str]
    packs: set[str]
    everything: bool = False


class ToolRouter:
    """Mantiene il toolset del turno e permette espansioni on-demand."""

    def __init__(self):
        self.selection = ToolSelection(set(PACKS["core"]), {"core"})

    def select(self, user_input: str, source: str = "cli",
               mode: str = "", available: set[str] | None = None) -> ToolSelection:
        router_mode = _router_mode()
        if router_mode == "off":
            # Nessun filtro: l'agente vede tutto cio' che ha davvero.
            names = set(available) if available else set().union(*PACKS.values())
            self.selection = ToolSelection(names, set(PACKS), everything=True)
            return self.selection

        text = str(user_input or "").lower()
        packs = {"core"} if router_mode == "strict" else set(ALWAYS_ON_PACKS)
        for pack, words in KEYWORDS.items():
            if any(word in text for word in words):
                packs.add(pack)
        if source in {"heartbeat", "cron"}:
            packs.update({"memory", "communication"})
        if mode == "implement":
            packs.add("files")
        names: set[str] = set()
        for pack in packs:
            names.update(PACKS.get(pack, set()))
        if available:
            names.intersection_update(available)
        self.selection = ToolSelection(names, packs)
        return self.selection

    def activate(self, packs: list[str], available: set[str]) -> tuple[set[str], list[str]]:
        unknown: list[str] = []
        for raw in packs:
            pack = str(raw or "").strip().lower()
            if pack == "all":
                self.selection.packs.update(PACKS)
                self.selection.names.update(available)
            elif pack in PACKS:
                self.selection.packs.add(pack)
                self.selection.names.update(PACKS[pack])
            else:
                unknown.append(pack)
        self.selection.names.intersection_update(available)
        return set(self.selection.names), unknown

    @staticmethod
    def mode() -> str:
        return _router_mode()

    @staticmethod
    def compact_index() -> str:
        return ", ".join(
            f"{name}({len(tools)})" for name, tools in PACKS.items()
            if name != "core"
        )
