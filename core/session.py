"""
openvurp Core — Session Management

Persistenza sessione, token tracking, history compaction.
"""

from __future__ import annotations

import os
import json
from datetime import datetime
import uuid
from dataclasses import dataclass, field, asdict
from typing import Optional

from core.tools import ToolResult


@dataclass
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    llm_calls: int = 0
    # Input token dell'ultima chiamata = dimensione reale del contesto
    # attuale (quando il backend fornisce usage reale).
    last_input_tokens: int = 0

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def estimated_cost(self) -> float:
        """Stima costo (basata su prezzi tipici)."""
        return (self.input_tokens * 0.003 + self.output_tokens * 0.015) / 1000

    def add_call(self, input_tokens: int = 0, output_tokens: int = 0):
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        if input_tokens > 0:
            self.last_input_tokens = input_tokens
        self.llm_calls += 1


class Session:
    def __init__(self, session_dir: str = ""):
        self.id = datetime.now().strftime("%Y%m%d_%H%M%S_%f") + "_" + uuid.uuid4().hex[:6]
        self.started_at = datetime.now()
        self.messages: list[dict] = []
        self.tool_history: list[dict] = []
        self.tokens = TokenUsage()
        self.session_dir = session_dir
        self.turns = 0

    def add_message(self, role: str, content: str):
        self.messages.append({
            "role": role,
            "content": content,
            "timestamp": datetime.now().isoformat()
        })
        if role == "user":
            self.turns += 1

    def add_tool_result(self, result: ToolResult, tool_name: str = "", args: dict = None):
        self.tool_history.append({
            "tool": tool_name or result.tool_name,
            "args": args or {},
            "success": result.success,
            "duration_ms": result.duration_ms,
            "error_type": result.error_type.value if result.error_type else None,
            "timestamp": datetime.now().isoformat()
        })

    def save(self):
        """Salva sessione su disco."""
        if not self.session_dir:
            return

        os.makedirs(self.session_dir, exist_ok=True)
        path = os.path.join(self.session_dir, f"{self.id}.json")

        data = {
            "id": self.id,
            "started_at": self.started_at.isoformat(),
            "ended_at": datetime.now().isoformat(),
            "turns": self.turns,
            "tokens": {
                "input": self.tokens.input_tokens,
                "output": self.tokens.output_tokens,
                "llm_calls": self.tokens.llm_calls,
                "estimated_cost": round(self.tokens.estimated_cost, 4)
            },
            "tool_calls": len(self.tool_history),
            "tools_used": list(set(t["tool"] for t in self.tool_history)),
            "errors": sum(1 for t in self.tool_history if not t["success"]),
            "duration_minutes": round(
                (datetime.now() - self.started_at).total_seconds() / 60, 1
            ),
            "last_user_message": self._last_message_preview("user"),
            "last_assistant_message": self._last_message_preview("assistant"),
            "recent_messages": self._recent_message_previews(),
            # Conversazione completa per continuità memoria
            "conversation": self._full_conversation(),
        }

        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)

    def summary(self) -> dict:
        """Riassunto sessione corrente."""
        tools_used = {}
        for t in self.tool_history:
            name = t["tool"]
            tools_used[name] = tools_used.get(name, 0) + 1

        return {
            "id": self.id,
            "turns": self.turns,
            "llm_calls": self.tokens.llm_calls,
            "tool_calls": len(self.tool_history),
            "tools_used": tools_used,
            "errors": sum(1 for t in self.tool_history if not t["success"]),
            "tokens_total": self.tokens.total,
            "estimated_cost": f"${self.tokens.estimated_cost:.4f}",
            "duration": str(datetime.now() - self.started_at).split('.')[0]
        }

    @classmethod
    def load(cls, path: str) -> Optional[dict]:
        """Carica summary sessione da file."""
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None

    @classmethod
    def load_last_conversation(cls, session_dir: str) -> list[dict]:
        """Carica i messaggi dell'ultima sessione per ripristinare la conversazione.

        Returns:
            Lista di dict {"role": ..., "content": ...} pronti per self.messages
        """
        if not session_dir or not os.path.exists(session_dir):
            return []

        # Trova la sessione più recente
        files = []
        for f in os.listdir(session_dir):
            if f.endswith(".json"):
                fp = os.path.join(session_dir, f)
                files.append((os.path.getmtime(fp), fp))

        if not files:
            return []

        files.sort(reverse=True)
        latest_path = files[0][1]

        data = cls.load(latest_path)
        if not data:
            return []

        # Usa il campo "conversation" (messaggi completi)
        conversation = data.get("conversation")
        if not isinstance(conversation, list) or not conversation:
            return []

        messages = []
        for item in conversation:
            role = item.get("role", "")
            text = item.get("text", "")
            if role in ("user", "assistant") and text:
                messages.append({"role": role, "content": text})

        return messages

    def _last_message_preview(self, role: str, max_chars: int = 240) -> str:
        """Ultimo messaggio di un ruolo, ridotto a preview singola riga."""
        for msg in reversed(self.messages):
            if msg.get("role") == role and msg.get("content"):
                return self._preview_text(msg["content"], max_chars=max_chars)
        return ""

    def _recent_message_previews(self, limit: int = 6, max_chars: int = 160) -> list[dict]:
        """Piccolo estratto degli ultimi messaggi non-system per memoria locale."""
        previews = []
        for msg in self.messages:
            role = msg.get("role")
            if role in ("system", "tool_result"):
                continue
            content = self._preview_text(msg.get("content", ""), max_chars=max_chars)
            if not content:
                continue
            previews.append({
                "role": role,
                "preview": content,
            })
        return previews[-limit:]

    def _full_conversation(self, max_chars_per_msg: int = 500, max_messages: int = 20) -> list[dict]:
        """Salva la conversazione con messaggi più completi per la memoria."""
        conv = []
        for msg in self.messages:
            role = msg.get("role")
            if role in ("system", "tool_result"):
                continue
            content = msg.get("content", "")
            if not content:
                continue
            text = " ".join(str(content).split())
            if len(text) > max_chars_per_msg:
                text = text[:max_chars_per_msg - 1] + "…"
            conv.append({"role": role, "text": text})
        return conv[-max_messages:]

    @staticmethod
    def _preview_text(text: str, max_chars: int = 160) -> str:
        compact = " ".join(str(text).split())
        if len(compact) <= max_chars:
            return compact
        return compact[:max_chars - 1] + "…"
