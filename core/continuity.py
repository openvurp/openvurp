"""
openvurp Core - Continuity prompt.

Pulls the active autonomy state, open loops, and recent reflections into a
small prompt section so long-running work resumes from durable context instead
of relying on the model's short-term chat memory.
"""

from __future__ import annotations

import re

from core.agent_state import AgentStateMachine
from core.security.audit import redact
from core.task_journal import OpenLoop, TaskJournal


class ContinuityPromptBuilder:
    def __init__(self, agent_state: AgentStateMachine, journal: TaskJournal):
        self.agent_state = agent_state
        self.journal = journal

    def build(self, user_input: str = "", session_type: str = "main",
              budget_chars: int = 5000) -> str:
        if session_type != "main":
            return ""

        sections = [self.agent_state.prompt_section()]

        loops = self._rank_open_loops(user_input, self.journal.list_open_loops())
        if loops:
            sections.append(self._format_open_loops(loops[:6]))

        reflections = self._recent_reflections(limit=6)
        if reflections:
            sections.append(self._format_reflections(reflections))

        text = "\n\n".join(part for part in sections if part.strip())
        return text[:budget_chars]

    def _rank_open_loops(self, user_input: str, loops: list[OpenLoop]) -> list[OpenLoop]:
        keywords = self._keywords(user_input)
        ranked: list[tuple[int, OpenLoop]] = []
        for loop in loops:
            haystack = f"{loop.title} {loop.description} {' '.join(loop.tags)}".lower()
            score = 0
            for keyword in keywords:
                if keyword in haystack:
                    score += haystack.count(keyword) + 2
            if loop.due:
                score += 1
            ranked.append((score, loop))
        ranked.sort(key=lambda item: (item[0], item[1].updated_at or item[1].created_at), reverse=True)
        return [loop for _, loop in ranked]

    def _recent_reflections(self, limit: int) -> list[dict]:
        try:
            return self.journal.review(max_reflections=limit).recent_reflections[-limit:]
        except Exception:
            return []

    def _format_open_loops(self, loops: list[OpenLoop]) -> str:
        lines = ["## OPEN LOOPS"]
        for loop in loops:
            tags = f" tags={','.join(loop.tags[:4])}" if loop.tags else ""
            due = f" due={loop.due}" if loop.due else ""
            lines.append(f"- [{loop.id}] {self._clean(loop.title, 140)}{due}{tags}")
            if loop.description:
                lines.append(f"  {self._clean(loop.description, 260)}")
        lines.append("Instruction: consider whether this turn should close, advance, or preserve any open loop.")
        return "\n".join(lines)

    def _format_reflections(self, reflections: list[dict]) -> str:
        lines = ["## RECENT REFLECTIONS"]
        for item in reflections:
            result = self._clean(item.get("result", ""), 180)
            intent = self._clean(item.get("user_intent", ""), 140)
            status = self._clean(item.get("status", ""), 40)
            tools = item.get("tools_used") or []
            failures = item.get("failures") or []
            line = f"- {status or 'turn'}"
            if intent:
                line += f": {intent}"
            if result:
                line += f" -> {result}"
            if tools:
                line += f" tools={','.join(str(tool) for tool in tools[:5])}"
            if failures:
                line += f" failures={','.join(str(failure) for failure in failures[:4])}"
            lines.append(line)
        lines.append("Instruction: reuse relevant lessons from these reflections, but do not narrate the memory lookup.")
        return "\n".join(lines)

    def _keywords(self, text: str) -> list[str]:
        stopwords = {
            "the", "and", "for", "con", "che", "non", "una", "uno", "gli",
            "della", "dello", "delle", "questo", "quello", "fammi", "vai",
            "continua", "procedi", "ok",
        }
        words = re.findall(r"[a-zA-Zà-ú0-9_./-]{3,}", (text or "").lower())
        return [word for word in words if word not in stopwords][:16]

    def _clean(self, text: str, max_chars: int) -> str:
        compact = " ".join(str(text or "").split())
        return redact(compact)[:max_chars]
