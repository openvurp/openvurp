"""
openvurp Tools — Plugin Scaffolding

Crea skeleton plugin in plugins/* così l'agente può trasformare workaround
ad hoc in capability riusabili e poi ricaricarle a runtime.
"""

from __future__ import annotations

import json
import os
import re

from core.tools import Tool, ToolResult, ErrorType


PLUGIN_ID_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
TOOL_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _openvurp_dir() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _indent_json(data: dict) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def scaffold_plugin_handler(
    plugin_id: str,
    tool_name: str,
    description: str,
    overwrite: bool = False,
) -> ToolResult:
    """Crea un plugin minimale con un tool placeholder."""
    plugin_id = (plugin_id or "").strip()
    tool_name = (tool_name or "").strip()
    description = (description or "").strip()

    if not plugin_id:
        return ToolResult.fail(
            "Parametro obbligatorio mancante: plugin_id",
            error_type=ErrorType.VALIDATION,
        )
    if not tool_name:
        return ToolResult.fail(
            "Parametro obbligatorio mancante: tool_name",
            error_type=ErrorType.VALIDATION,
        )
    if not description:
        return ToolResult.fail(
            "Parametro obbligatorio mancante: description",
            error_type=ErrorType.VALIDATION,
        )

    if not PLUGIN_ID_RE.match(plugin_id):
        return ToolResult.fail(
            "Invalid plugin_id. Use only letters, numbers and underscore, not starting with a number.",
            error_type=ErrorType.VALIDATION,
        )
    if not TOOL_NAME_RE.match(tool_name):
        return ToolResult.fail(
            "Invalid tool_name. Use a simple Python identifier.",
            error_type=ErrorType.VALIDATION,
        )

    root = _openvurp_dir()
    plugin_dir = os.path.join(root, "plugins", plugin_id)
    manifest_path = os.path.join(plugin_dir, "manifest.json")
    init_path = os.path.join(plugin_dir, "__init__.py")

    if os.path.exists(plugin_dir) and not overwrite:
        return ToolResult.fail(
            f"Plugin plugins/{plugin_id} already exists. Use overwrite=true to regenerate it.",
            error_type=ErrorType.VALIDATION,
        )

    os.makedirs(plugin_dir, exist_ok=True)

    handler_name = f"{tool_name}_handler"
    manifest = {
        "id": plugin_id,
        "name": plugin_id.replace("_", " ").title(),
        "version": "0.1.0",
        "description": description,
        "hooks": [],
        "tools": [
            {
                "name": tool_name,
                "description": description,
                "handler": handler_name,
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                },
            }
        ],
        "enabled": True,
    }

    module = f'''"""
Plugin `{plugin_id}` scaffoldato da openvurp.

Implementa il tool `{tool_name}` qui. Per il ciclo della fucina (forge)
implementa anche `selftest()`: deve PROVARE che il tool funziona davvero
(chiamarlo con input reali e verificare l'output), ritornando True o
sollevando un'eccezione. Senza selftest verde la capacità non si adotta.
"""

from __future__ import annotations

from core.tools import ToolResult, ErrorType


def register(_manager):
    return None


def unregister(_manager):
    return None


def {handler_name}(**_args):
    return ToolResult.fail(
        "Plugin scaffolato ma non ancora implementato.",
        error_type=ErrorType.RUNTIME,
    )


def selftest():
    raise NotImplementedError(
        "Scrivi un selftest vero: chiama {handler_name} con input reali "
        "e verifica il risultato."
    )
'''

    try:
        with open(manifest_path, "w", encoding="utf-8") as f:
            f.write(_indent_json(manifest))
        with open(init_path, "w", encoding="utf-8") as f:
            f.write(module)
    except OSError as exc:
        return ToolResult.fail(
            f"Cannot create the plugin: {exc}",
            error_type=ErrorType.RUNTIME,
        )

    output = (
        f"Plugin scaffolato in plugins/{plugin_id}.\n"
        f"- Manifest: plugins/{plugin_id}/manifest.json\n"
        f"- Modulo: plugins/{plugin_id}/__init__.py\n"
        f"- Tool dichiarato: {tool_name}\n"
        "Implementa l'handler placeholder e poi usa `reload_plugins`."
    )
    return ToolResult.ok(output)


SCAFFOLD_PLUGIN_TOOL = Tool(
    name="scaffold_plugin",
    description=(
        "Crea uno skeleton plugin in plugins/ con manifest e handler placeholder. "
        "Usalo quando ti serve trasformare una soluzione ad hoc in una capability riusabile."
    ),
    parameters={
        "type": "object",
        "properties": {
            "plugin_id": {
                "type": "string",
                "description": "Identificatore del plugin, es. desktop_tools",
            },
            "tool_name": {
                "type": "string",
                "description": "Nome del tool che il plugin esporrà, es. desktop_screenshot",
            },
            "description": {
                "type": "string",
                "description": "Descrizione breve e concreta del tool.",
            },
            "overwrite": {
                "type": "boolean",
                "description": "Rigenera il plugin se esiste già.",
            },
        },
        "required": ["plugin_id", "tool_name", "description"],
    },
    handler=scaffold_plugin_handler,
)
