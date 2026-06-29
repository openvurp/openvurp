"""
openvurp Core — Tool Registry

Sistema strutturato di tool con schema, lifecycle hooks, retry policy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional


class ErrorType(Enum):
    NONE = "none"
    TIMEOUT = "timeout"
    PERMISSION = "permission"
    NOT_FOUND = "not_found"
    DEPENDENCY = "dependency"
    RUNTIME = "runtime"
    NETWORK = "network"
    VALIDATION = "validation"


@dataclass
class RetryPolicy:
    max_retries: int = 0
    backoff_seconds: float = 1.0
    retryable_errors: list[ErrorType] = field(default_factory=lambda: [
        ErrorType.TIMEOUT, ErrorType.NETWORK
    ])


@dataclass
class ToolResult:
    success: bool
    output: str
    error: Optional[str] = None
    error_type: ErrorType = ErrorType.NONE
    duration_ms: int = 0
    retryable: bool = False
    tool_name: str = ""

    @staticmethod
    def ok(output: str, duration_ms: int = 0, tool_name: str = "") -> "ToolResult":
        return ToolResult(
            success=True, output=output,
            duration_ms=duration_ms, tool_name=tool_name
        )

    @staticmethod
    def fail(error: str, error_type: ErrorType = ErrorType.RUNTIME,
             output: str = "", duration_ms: int = 0,
             retryable: bool = False, tool_name: str = "") -> "ToolResult":
        return ToolResult(
            success=False, output=output, error=error,
            error_type=error_type, duration_ms=duration_ms,
            retryable=retryable, tool_name=tool_name
        )


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict = field(default_factory=dict)  # JSON Schema
    requires_approval: bool = False
    timeout: int = 120
    retry_policy: RetryPolicy = field(default_factory=RetryPolicy)
    handler: Optional[Callable] = None

    def schema_for_prompt(self) -> str:
        """Genera descrizione leggibile per il prompt LLM."""
        lines = [f"### {self.name}"]
        lines.append(self.description)
        if self.parameters:
            lines.append("Parametri:")
            props = self.parameters.get("properties", {})
            required = self.parameters.get("required", [])
            for pname, pinfo in props.items():
                req = " (obbligatorio)" if pname in required else ""
                desc = pinfo.get("description", "")
                ptype = pinfo.get("type", "string")
                lines.append(f"  - {pname} ({ptype}){req}: {desc}")
        if self.requires_approval:
            lines.append("⚠ Richiede approvazione utente")
        return "\n".join(lines)


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool):
        self._tools[tool.name] = tool

    def unregister(self, name: str):
        self._tools.pop(name, None)

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def list(self) -> list[Tool]:
        return list(self._tools.values())

    def names(self) -> list[str]:
        return list(self._tools.keys())

    def prompt_section(self, native_tools: bool = False) -> str:
        """Genera la sezione tool per il system prompt.

        Con function calling nativo (native_tools=True) gli schemi dei tool
        vengono passati al modello dal runtime via API: ripetere qui il
        formato testuale ```TOOL: confonde il modello e produce chiamate
        malformate (es. ```TOOL:read_file"> ). In quel caso non emettiamo
        nulla — la fonte di verità sono gli schemi nativi.
        """
        if not self._tools or native_tools:
            return ""
        lines = ["## TOOL DISPONIBILI\n"]
        lines.append("Usa i blocchi ```TOOL:nome_tool per invocare un tool.")
        lines.append("Il contenuto del blocco è un JSON con i parametri.\n")
        lines.append(
            "Preferisci i tool strutturati per leggere, cercare e modificare nel workspace. "
            "Usa ```SHELL per test, git, package manager e comandi reali del sistema.\n"
        )
        for tool in self._tools.values():
            lines.append(tool.schema_for_prompt())
            lines.append("")
        lines.append("Formato invocazione:")
        lines.append("```TOOL:nome_tool")
        lines.append('{"param1": "valore1", "param2": "valore2"}')
        lines.append("```")
        lines.append("")
        lines.append("Oppure per shell semplice (retrocompatibile):")
        lines.append("```SHELL")
        lines.append("comando")
        lines.append("```")
        return "\n".join(lines)

    def to_openai_schema(self) -> list[dict]:
        """Genera schema tool per OpenAI/Groq function calling."""
        tools = []
        for tool in self._tools.values():
            schema = {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters or {
                        "type": "object", "properties": {}, "required": []
                    },
                }
            }
            tools.append(schema)
        return tools

    def to_anthropic_schema(self) -> list[dict]:
        """Genera schema tool per Anthropic function calling."""
        tools = []
        for tool in self._tools.values():
            schema = {
                "name": tool.name,
                "description": tool.description,
                "input_schema": tool.parameters or {
                    "type": "object", "properties": {}, "required": []
                },
            }
            tools.append(schema)
        return tools
