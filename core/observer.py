"""
openvurp Core — Observer

Structured logging, metrics, session trace.
"""

from __future__ import annotations

import os
import json
import time
from datetime import datetime
from typing import Optional

from core.tools import ToolResult


class Observer:
    def __init__(self, log_dir: str = "logs/"):
        self.log_dir = log_dir
        self.session_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.events: list[dict] = []
        self.start_time = time.time()

        # Contatori rapidi
        self.llm_calls = 0
        self.tool_calls = 0
        self.errors = 0
        self.total_llm_ms = 0
        self.total_tool_ms = 0
        self.tokens_in = 0
        self.tokens_out = 0

    def _event(self, event_type: str, data: dict):
        """Registra un evento."""
        self.events.append({
            "type": event_type,
            "timestamp": datetime.now().isoformat(),
            "elapsed_ms": int((time.time() - self.start_time) * 1000),
            **data
        })

    def log_llm_call(self, messages_count: int, response_len: int,
                     duration_ms: int, input_tokens: int = 0, output_tokens: int = 0):
        """Log chiamata LLM."""
        self.llm_calls += 1
        self.total_llm_ms += duration_ms
        self.tokens_in += input_tokens
        self.tokens_out += output_tokens

        self._event("llm_call", {
            "messages": messages_count,
            "response_chars": response_len,
            "duration_ms": duration_ms,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        })

    def log_tool_call(self, tool_name: str, args: dict, result: ToolResult):
        """Log esecuzione tool."""
        self.tool_calls += 1
        self.total_tool_ms += result.duration_ms
        if not result.success:
            self.errors += 1

        self._event("tool_call", {
            "tool": tool_name,
            "args_keys": list(args.keys()),
            "success": result.success,
            "duration_ms": result.duration_ms,
            "error_type": result.error_type.value if result.error_type else None,
            "output_len": len(result.output),
        })

    def log_plan(self, goal: str, steps_count: int):
        """Log creazione piano."""
        self._event("plan_created", {
            "goal": goal[:200],
            "steps": steps_count,
        })

    def log_error(self, error: str, context: str = ""):
        """Log errore."""
        self.errors += 1
        self._event("error", {
            "error": str(error)[:500],
            "context": context[:200],
        })

    def log_decision(self, what: str, why: str):
        """Log decisione dell'agente."""
        self._event("decision", {
            "what": what[:200],
            "why": why[:200],
        })

    def log_thinking_level(self, level: str, user_input: str):
        """Log classificazione thinking level."""
        self._event("thinking_level", {
            "level": level,
            "input_preview": user_input[:100],
        })

    def save_session_trace(self):
        """Salva trace completo della sessione."""
        if not self.events:
            return

        os.makedirs(self.log_dir, exist_ok=True)
        path = os.path.join(self.log_dir, f"trace_{self.session_id}.json")

        trace = {
            "session_id": self.session_id,
            "started_at": datetime.fromtimestamp(self.start_time).isoformat(),
            "ended_at": datetime.now().isoformat(),
            "summary": self.summary(),
            "events": self.events,
        }

        with open(path, "w", encoding="utf-8") as f:
            json.dump(trace, f, indent=2, ensure_ascii=False)

    def summary(self) -> dict:
        """Riassunto sessione."""
        elapsed = time.time() - self.start_time

        # Tool breakdown
        tools_used = {}
        for e in self.events:
            if e["type"] == "tool_call":
                name = e["tool"]
                tools_used[name] = tools_used.get(name, 0) + 1

        return {
            "duration": f"{elapsed:.0f}s",
            "llm_calls": self.llm_calls,
            "llm_time_ms": self.total_llm_ms,
            "tool_calls": self.tool_calls,
            "tool_time_ms": self.total_tool_ms,
            "errors": self.errors,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "tokens_total": self.tokens_in + self.tokens_out,
            "tools_used": tools_used,
            "events_count": len(self.events),
        }

    def format_trace(self) -> str:
        """Formatta il trace per display CLI (/trace command)."""
        s = self.summary()
        lines = [
            f"  Sessione: {self.session_id}",
            f"  Durata:   {s['duration']}",
            f"",
            f"  LLM calls:  {s['llm_calls']}  ({s['llm_time_ms']}ms)",
            f"  Tool calls: {s['tool_calls']}  ({s['tool_time_ms']}ms)",
            f"  Errori:     {s['errors']}",
            f"",
            f"  Token in:   {s['tokens_in']}",
            f"  Token out:  {s['tokens_out']}",
            f"  Token tot:  {s['tokens_total']}",
        ]

        if s['tools_used']:
            lines.append(f"")
            lines.append(f"  Tool usati:")
            for name, count in sorted(s['tools_used'].items(), key=lambda x: -x[1]):
                lines.append(f"    {name}: {count}")

        return "\n".join(lines)
