"""
openvurp Core - Agent kernel.

This layer turns "be agentic" from a prompt preference into runtime policy.
It decides which turns need action, builds a concrete plan, and blocks final
answers that arrive before observation or verification.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import re
from typing import Any


class KernelMode(Enum):
    CHAT = "chat"
    ANSWER = "answer"
    INVESTIGATE = "investigate"
    IMPLEMENT = "implement"
    OPERATE = "operate"


class GateAction(Enum):
    ALLOW = "allow"
    CONTINUE = "continue"


@dataclass
class KernelPlan:
    goal: str
    mode: str
    thinking_level: str = "normal"
    steps: list[str] = field(default_factory=list)
    requires_tools: bool = False
    requires_verification: bool = False
    verification_hint: str = ""

    @property
    def active(self) -> bool:
        return self.mode not in {KernelMode.CHAT.value, KernelMode.ANSWER.value}


@dataclass
class ToolTraceSummary:
    tools_used: list[str] = field(default_factory=list)
    failed_tools: list[str] = field(default_factory=list)
    inspected: bool = False
    wrote: bool = False
    verified: bool = False


@dataclass
class FinalGate:
    action: str
    reason: str = ""
    prompt: str = ""

    @property
    def allowed(self) -> bool:
        return self.action == GateAction.ALLOW.value


class AgentKernel:
    INSPECTION_TOOLS = {
        "read_file", "grep", "find_files", "glob", "shell", "web_search",
        "web_fetch", "browser", "browser_devtools", "desktop_screenshot",
        "doctor", "process_list", "process_sessions",
    }
    WRITE_TOOLS = {
        "write_file", "edit_file", "edit_lines", "append_file",
        "evolve_self", "delete_bootstrap", "scaffold_plugin",
    }
    VERIFY_TOOLS = {
        "shell", "doctor", "browser", "browser_devtools", "process_read",
        "learning_review", "task_journal", "agent_state",
    }

    QUICK_ACK = {
        "ok", "va bene", "grazie", "perfetto", "si", "sì", "no",
        "ciao", "bene",
    }
    # Chiusure sociali / presenza emotiva: NON vanno filtrate dalla modalità
    # investigate. Una persona che saluta, ringrazia o si lamenta della
    # mia presenza merita una risposta umana, non un'indagine.
    SOCIAL_PATTERNS = (
        r"^buonanotte\b", r"^buongiorno\b", r"^buonasera\b",
        r"^ciao\b", r"^salve\b", r"^ehi\b", r"^hey\b",
        r"grazie[.! ]*$", r"^prego[.! ]*$",
        r"^a dopo\b", r"^a presto\b", r"^ci sentiamo\b",
        r"^ok[.! ]*$", r"^perfetto[.! ]*$", r"^bene[.! ]*$",
        r"^bene così\b",
    )
    # Lamentele/meta sulla presenza dell'agente: troppo vaghe per essere
    # task operative, vanno trattate come conversazione.
    PRESENCE_COMPLAINTS = (
        "non rispondi", "non mi rispondi", "non mi parli",
        "sei sparito", "sparito", "dove sei", "ci sei",
        "non ti sento", "perché non", "perche non", "why don't you",
        "why not",
    )
    CONTINUE_MARKERS = {
        "vai", "continua", "procedi", "prosegui", "fallo", "andiamo avanti",
        "fai tutto",
    }

    IMPLEMENT_WORDS = {
        "implementa", "aggiungi", "aggiorna", "modifica", "correggi",
        "fixa", "sistema", "risolvi", "crea", "costruisci", "scrivi",
        "refactor", "rifattorizza", "integra", "porta",
    }
    INVESTIGATE_WORDS = {
        "analizza", "controlla", "verifica", "debugga", "investiga",
        "trova", "cerca", "confronta", "perche", "perché", "cosa sbaglio",
    }
    OPERATE_WORDS = {
        "installa", "configura", "avvia", "lancia", "deploy", "reset",
        "testa", "esegui", "scarica",
    }
    PROJECT_WORDS = {
        "progetto", "codice", "repo", "file", "cartella", "runtime",
        "agente", "openvurp", "openvurp", "tool", "test", "github",
    }

    def prepare(
        self,
        user_input: str,
        thinking_level: str = "normal",
        tools_available: list[str] | None = None,
        active_goal: str = "",
        is_addressed: bool = True,
    ) -> KernelPlan:
        """Compute the kernel plan for one turn.

        Args:
            is_addressed: True when the message is meant for the agent (private
                chat, @mention in a group, reply to bot). The runtime MUST pass
                this so the kernel can decide whether silence is even possible.
                In group chats where the agent is NOT addressed, the runtime
                should not even call prepare (or should pass is_addressed=False
                so the kernel can short-circuit to a no-op response).
        """
        goal = self._goal_for_turn(user_input, active_goal)
        mode = self._classify(goal, thinking_level)
        tools = set(tools_available or [])

        # Hard rule: if the runtime says the agent is not addressed, we do
        # NOT fabricate a response. The runtime should not even reach us in
        # that case, but if it does (e.g. moderation mode, watch), we force
        # CHAT with no tool requirement so any output stays a quiet ack.
        if not is_addressed:
            return KernelPlan(
                goal=goal,
                mode=KernelMode.CHAT.value,
                thinking_level=thinking_level,
            )

        if mode == KernelMode.CHAT:
            return KernelPlan(goal=goal, mode=mode.value, thinking_level=thinking_level)

        requires_tools = mode in {KernelMode.INVESTIGATE, KernelMode.IMPLEMENT, KernelMode.OPERATE}
        requires_verification = mode in {KernelMode.IMPLEMENT, KernelMode.OPERATE}
        if mode == KernelMode.INVESTIGATE and self._mentions_project(goal):
            requires_tools = bool(tools)

        steps = self._build_steps(mode, goal)
        return KernelPlan(
            goal=goal,
            mode=mode.value,
            thinking_level=thinking_level,
            steps=steps,
            requires_tools=requires_tools,
            requires_verification=requires_verification,
            verification_hint=self._verification_hint(mode),
        )

    def prompt_section(self, plan: KernelPlan | None) -> str:
        if not plan or not plan.active:
            return (
                "## AGENT KERNEL\n"
                "For simple chat, answer normally. For any real task, the runtime expects: "
                "inspect -> plan -> act -> observe -> verify -> finish."
            )

        lines = [
            "## AGENT KERNEL",
            "This is runtime policy, not personality advice.",
            f"Mode: {plan.mode}",
            f"Goal: {plan.goal}",
            f"Requires tools: {'yes' if plan.requires_tools else 'no'}",
            f"Requires verification: {'yes' if plan.requires_verification else 'no'}",
            "Do not produce a final answer until the required observation and verification are done.",
        ]
        if plan.steps:
            lines.append("Runtime plan:")
            for idx, step in enumerate(plan.steps[:8], start=1):
                lines.append(f"{idx}. {step}")
        if plan.verification_hint:
            lines.append(f"Verification hint: {plan.verification_hint}")
        lines.append(
            "If you cannot continue safely, ask for the smallest missing decision and leave an open loop."
        )
        return "\n".join(lines)

    def review_final(
        self,
        plan: KernelPlan | None,
        final_text: str,
        tool_history: list[dict],
        waiting_user: bool = False,
        interventions: int = 0,
    ) -> FinalGate:
        if not plan or not plan.active:
            return FinalGate(GateAction.ALLOW.value)
        if waiting_user:
            return FinalGate(GateAction.ALLOW.value)
        # Un solo intervento per turno: il kernel dà una spinta, non un muro.
        # Oltre questo, fidati del modello — i gate ripetuti degradano la
        # risposta più di quanto la migliorino.
        if interventions >= 1:
            return FinalGate(GateAction.ALLOW.value)

        summary = self.summarize_tools(tool_history)
        if plan.requires_tools and not summary.tools_used:
            return self._continue_gate(
                plan,
                "No tool observation exists for an actionable task.",
                "Inspect the relevant workspace/context with tools before answering.",
            )

        if summary.failed_tools and not self._acknowledges_blocker(final_text):
            return self._continue_gate(
                plan,
                "A tool failed and the answer does not handle the blocker.",
                "Revise the approach, retry with a safer tool, or ask for the missing decision.",
            )

        if plan.mode == KernelMode.INVESTIGATE.value and not summary.inspected:
            return self._continue_gate(
                plan,
                "The task requires investigation but no inspection tool was used.",
                "Read or search the relevant files/context before giving conclusions.",
            )

        if plan.requires_verification:
            if summary.wrote and not summary.verified:
                return self._continue_gate(
                    plan,
                    "Changes were made without a verification step.",
                    plan.verification_hint or "Run the smallest relevant verification before finishing.",
                )
            # Nota: niente gate su "claims completion" quando non è stato
            # scritto nulla — il match a substring ("fatto", "done", ...)
            # produceva falsi blocchi su risposte legittime.

        return FinalGate(GateAction.ALLOW.value)

    def summarize_tools(self, tool_history: list[dict]) -> ToolTraceSummary:
        summary = ToolTraceSummary()
        for item in tool_history or []:
            name = str(item.get("tool") or "")
            if name and name not in summary.tools_used:
                summary.tools_used.append(name)
            if not item.get("success", True):
                summary.failed_tools.append(name or "tool")
            if name in self.INSPECTION_TOOLS:
                summary.inspected = True
            if name in self.WRITE_TOOLS or self._shell_looks_like_write(item):
                summary.wrote = True
            if name in self.VERIFY_TOOLS and self._tool_looks_like_verification(item):
                summary.verified = True
        return summary

    def should_track_open_loop(
        self,
        plan: KernelPlan | None,
        final_text: str,
        waiting_user: bool,
    ) -> bool:
        if not plan or not plan.active:
            return False
        if waiting_user:
            return True
        text = (final_text or "").lower()
        return any(marker in text for marker in (
            "rimane", "manca", "prossimo", "next", "follow-up",
            "bloccato", "serve", "aspetto",
        ))

    def _goal_for_turn(self, user_input: str, active_goal: str) -> str:
        text = " ".join((user_input or "").split())
        if text.lower() in self.CONTINUE_MARKERS and active_goal:
            return active_goal
        return text

    def _classify(self, goal: str, thinking_level: str) -> KernelMode:
        text = (goal or "").strip().lower()
        if not text or text in self.QUICK_ACK:
            return KernelMode.CHAT
        # I saluti, le chiusure sociali e le lamentele sulla mia presenza
        # sono conversazione, non task operative. Non devono finire in
        # INVESTIGATE anche se contengono "perché" — altrimenti il gate
        # blocca la risposta e l'utente resta in silenzio.
        if self._looks_like_social(text):
            return KernelMode.CHAT
        if any(word in text for word in self.IMPLEMENT_WORDS):
            return KernelMode.IMPLEMENT
        if any(word in text for word in self.OPERATE_WORDS):
            return KernelMode.OPERATE
        if any(word in text for word in self.INVESTIGATE_WORDS):
            return KernelMode.INVESTIGATE
        if (thinking_level or "").lower() == "deep" and self._mentions_project(text):
            return KernelMode.INVESTIGATE
        if self._looks_like_question(text):
            return KernelMode.ANSWER
        return KernelMode.ANSWER

    def _build_steps(self, mode: KernelMode, goal: str) -> list[str]:
        if mode == KernelMode.IMPLEMENT:
            return [
                "Inspect the relevant code, tests, and existing patterns.",
                "Define the smallest concrete change that satisfies the goal.",
                "Edit the owned files without unrelated refactors.",
                "Run the narrowest useful verification.",
                "Record open loops or reflections if anything remains.",
                "Finish with outcome, verification, and remaining gaps.",
            ]
        if mode == KernelMode.OPERATE:
            return [
                "Inspect the current runtime/environment state.",
                "Choose the least risky command or tool action.",
                "Execute the action.",
                "Verify the resulting state.",
                "Record blockers or follow-ups if not fully resolved.",
                "Finish with what changed and what is still needed.",
            ]
        if mode == KernelMode.INVESTIGATE:
            return [
                "Inspect the relevant local or external evidence.",
                "Compare evidence against the user's concern.",
                "Identify root causes, weak spots, and concrete fixes.",
                "Verify claims against observed files or outputs.",
                "Finish with prioritized next actions.",
            ]
        return [
            "Understand the request.",
            "Answer directly.",
        ]

    def _verification_hint(self, mode: KernelMode) -> str:
        if mode == KernelMode.IMPLEMENT:
            return "Run targeted tests, compile checks, lint, or the smallest command that proves the changed behavior."
        if mode == KernelMode.OPERATE:
            return "Check command status, process state, files, logs, or doctor output after the action."
        if mode == KernelMode.INVESTIGATE:
            return "Cite or rely on inspected files, tool output, or fetched evidence."
        return ""

    def _continue_gate(self, plan: KernelPlan, reason: str, next_action: str) -> FinalGate:
        prompt = (
            "[KERNEL CHECK]\n"
            "Do not answer final yet.\n"
            f"Goal: {plan.goal}\n"
            f"Reason: {reason}\n"
            f"Next action: {next_action}\n"
            "Use the appropriate tool now, then observe and decide whether the goal is actually complete."
        )
        return FinalGate(GateAction.CONTINUE.value, reason=reason, prompt=prompt)

    def _mentions_project(self, text: str) -> bool:
        lowered = (text or "").lower()
        return any(word in lowered for word in self.PROJECT_WORDS)

    def _looks_like_question(self, text: str) -> bool:
        return "?" in text or text.startswith((
            "cosa ", "come ", "perche ", "perché ", "why ", "what ", "how ",
        ))

    def _looks_like_social(self, text: str) -> bool:
        """True for greetings, closings, thanks, and presence complaints.

        These are conversational — not actionable tasks — so they must skip
        the investigate/operate classifier entirely. The previous design let
        "perché non rispondi alla buonanotte?" fall through to INVESTIGATE,
        which silently swallowed a greeting because no inspection tool was
        used in the same turn.
        """
        lowered = (text or "").strip().lower()
        if not lowered:
            return False
        if any(re.search(pat, lowered) for pat in self.SOCIAL_PATTERNS):
            return True
        # Lamentele di presenza: se la domanda parla della mia presenza e
        # non cita un progetto/file, è conversazione.
        if any(marker in lowered for marker in self.PRESENCE_COMPLAINTS):
            if not self._mentions_project(lowered):
                return True
        return False

    def _shell_looks_like_write(self, item: dict[str, Any]) -> bool:
        if str(item.get("tool") or "") != "shell":
            return False
        command = str((item.get("args") or {}).get("command") or "").lower()
        write_markers = (
            "apply_patch", "cat >", "tee ", "sed -i", "python -c", "python3 -c",
            "npm install", "pip install", "touch ", "mkdir ", "mv ", "cp ",
        )
        return any(marker in command for marker in write_markers)

    def _tool_looks_like_verification(self, item: dict[str, Any]) -> bool:
        name = str(item.get("tool") or "")
        if name in {"doctor", "browser", "browser_devtools", "process_read", "learning_review"}:
            return bool(item.get("success", True))
        if name == "task_journal" or name == "agent_state":
            return False
        if name != "shell":
            return False
        command = str((item.get("args") or {}).get("command") or "").lower()
        verify_markers = (
            "test", "pytest", "unittest", "compileall", "ruff", "mypy",
            "tsc", "eslint", "npm run", "pnpm", "cargo test", "go test",
            "secret_scan", "doctor", "reset.py --list",
        )
        return any(marker in command for marker in verify_markers)

    def _acknowledges_blocker(self, text: str) -> bool:
        lowered = (text or "").lower()
        return any(marker in lowered for marker in (
            "blocc", "errore", "fallit", "non posso", "non riesco",
            "serve", "manca", "permission", "permesso",
        ))

    def _sounds_like_completed_work(self, text: str) -> bool:
        lowered = (text or "").lower()
        return any(marker in lowered for marker in (
            "fatto", "completato", "implementato", "aggiornato",
            "risolto", "sistemato", "done", "fixed", "finished",
        ))
