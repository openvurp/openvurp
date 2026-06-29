"""
openvurp Core — Plugin System

Sistema plugin con hook, caricamento dinamico, e tool registration.
I plugin vivono in plugins/*/ e hanno un manifest.json + __init__.py.
"""

from __future__ import annotations

import os
import json
import importlib
import importlib.util
from dataclasses import dataclass, field
from types import ModuleType
from typing import Any, Optional


# Hook disponibili
AVAILABLE_HOOKS = [
    "before_llm_call",      # (messages) -> messages
    "after_llm_response",   # (response) -> response
    "before_tool_call",     # (tool_name, args) -> (tool_name, args) o None per bloccare
    "after_tool_call",      # (tool_name, result) -> result
    "on_session_start",     # (session) -> None
    "on_session_end",       # (session) -> None
]


@dataclass
class PluginManifest:
    id: str
    name: str
    version: str = "0.1.0"
    description: str = ""
    hooks: list[str] = field(default_factory=list)
    tools: list[dict] = field(default_factory=list)
    enabled: bool = True

    @classmethod
    def from_dict(cls, data: dict) -> "PluginManifest":
        return cls(
            id=data.get("id", "unknown"),
            name=data.get("name", data.get("id", "Unknown")),
            version=data.get("version", "0.1.0"),
            description=data.get("description", ""),
            hooks=data.get("hooks", []),
            tools=data.get("tools", []),
            enabled=data.get("enabled", True),
        )

    @classmethod
    def from_file(cls, path: str) -> "PluginManifest":
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return cls.from_dict(data)


@dataclass
class Plugin:
    manifest: PluginManifest
    module: Optional[ModuleType] = None
    path: str = ""
    load_error: str = ""

    @property
    def loaded(self) -> bool:
        return self.module is not None

    @property
    def id(self) -> str:
        return self.manifest.id


class PluginManager:
    """Gestisce scoperta, caricamento e invocazione dei plugin."""

    def __init__(self, plugins_dir: str = "plugins"):
        self.plugins_dir = plugins_dir
        self.loaded: dict[str, Plugin] = {}
        self._hook_registry: dict[str, list[Plugin]] = {h: [] for h in AVAILABLE_HOOKS}
        self._dynamic_tools: dict[str, list] = {}
        self._loading_plugin_id: str = ""

    def discover(self) -> list[str]:
        """Scansiona plugins_dir per plugin disponibili."""
        found = []
        if not os.path.exists(self.plugins_dir):
            return found

        for entry in sorted(os.listdir(self.plugins_dir)):
            plugin_dir = os.path.join(self.plugins_dir, entry)
            if not os.path.isdir(plugin_dir):
                continue

            manifest_path = os.path.join(plugin_dir, "manifest.json")
            init_path = os.path.join(plugin_dir, "__init__.py")

            if os.path.exists(manifest_path) and os.path.exists(init_path):
                found.append(entry)

        return found

    def load(self, plugin_id: str) -> Plugin:
        """Carica un plugin dalla directory."""
        if plugin_id in self.loaded:
            return self.loaded[plugin_id]

        plugin_dir = os.path.join(self.plugins_dir, plugin_id)
        manifest_path = os.path.join(plugin_dir, "manifest.json")
        init_path = os.path.join(plugin_dir, "__init__.py")

        if not os.path.exists(manifest_path):
            manifest = PluginManifest(id=plugin_id, name=plugin_id)
            plugin = Plugin(manifest=manifest, path=plugin_dir,
                           load_error=f"manifest.json non trovato in {plugin_dir}")
            return plugin

        try:
            manifest = PluginManifest.from_file(manifest_path)
        except Exception as e:
            manifest = PluginManifest(id=plugin_id, name=plugin_id)
            plugin = Plugin(manifest=manifest, path=plugin_dir,
                           load_error=f"Errore manifest: {e}")
            return plugin

        if not manifest.enabled:
            plugin = Plugin(manifest=manifest, path=plugin_dir,
                           load_error="Plugin disabilitato")
            return plugin

        # Carica modulo Python
        try:
            spec = importlib.util.spec_from_file_location(
                f"plugins.{plugin_id}", init_path
            )
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)

            plugin = Plugin(manifest=manifest, module=module, path=plugin_dir)

            # Chiama register() se esiste
            if hasattr(module, 'register'):
                self._loading_plugin_id = manifest.id or plugin_id
                try:
                    module.register(self)
                finally:
                    self._loading_plugin_id = ""

            # Registra nei hooks
            for hook_name in manifest.hooks:
                if hook_name in self._hook_registry:
                    self._hook_registry[hook_name].append(plugin)

            self.loaded[plugin_id] = plugin
            return plugin

        except Exception as e:
            plugin = Plugin(manifest=manifest, path=plugin_dir,
                           load_error=f"Errore caricamento: {e}")
            return plugin

    def unload(self, plugin_id: str) -> bool:
        """Scarica un plugin."""
        plugin = self.loaded.pop(plugin_id, None)
        if not plugin:
            return False

        # Rimuovi dai hooks
        for hook_list in self._hook_registry.values():
            hook_list[:] = [p for p in hook_list if p.id != plugin_id]

        # Chiama unregister() se esiste
        if plugin.module and hasattr(plugin.module, 'unregister'):
            try:
                plugin.module.unregister(self)
            except Exception:
                pass

        self._dynamic_tools.pop(plugin_id, None)
        self._dynamic_tools.pop(plugin.id, None)

        return True

    def load_all(self):
        """Carica tutti i plugin scoperti."""
        for plugin_id in self.discover():
            self.load(plugin_id)

    def fire(self, hook_name: str, **kwargs) -> dict:
        """Esegue un hook su tutti i plugin registrati.

        Returns dict con risultati per plugin.
        I plugin possono modificare kwargs in-place.
        """
        results = {}
        plugins = self._hook_registry.get(hook_name, [])

        for plugin in plugins:
            if not plugin.loaded:
                continue

            handler = getattr(plugin.module, hook_name, None)
            if handler is None:
                continue

            try:
                result = handler(**kwargs)
                results[plugin.id] = result
            except Exception as e:
                results[plugin.id] = {"error": str(e)}

        return results

    def fire_chain(self, hook_name: str, value, **kwargs):
        """Esegue hook a catena: l'output di uno diventa input del successivo."""
        plugins = self._hook_registry.get(hook_name, [])

        for plugin in plugins:
            if not plugin.loaded:
                continue

            handler = getattr(plugin.module, hook_name, None)
            if handler is None:
                continue

            try:
                result = handler(value, **kwargs)
                if result is not None:
                    value = result
            except Exception:
                continue

        return value

    def get_tools(self) -> list:
        """Raccoglie tool da tutti i plugin caricati."""
        from core.tools import Tool

        tools = []
        for plugin in self.loaded.values():
            if not plugin.loaded:
                continue

            # Tool definiti nel manifest
            for tool_def in plugin.manifest.tools:
                if not isinstance(tool_def, dict):
                    continue
                handler = None
                if plugin.module:
                    handler = getattr(plugin.module, tool_def.get("handler", ""), None)

                if handler:
                    tool = Tool(
                        name=tool_def.get("name", f"{plugin.id}_{tool_def.get('handler', 'unknown')}"),
                        description=tool_def.get("description", "Plugin tool"),
                        parameters=tool_def.get("parameters", {}),
                        handler=handler,
                    )
                    tools.append(tool)

            # Tool registrati via get_tools()
            if hasattr(plugin.module, 'get_tools'):
                try:
                    plugin_tools = plugin.module.get_tools()
                    if isinstance(plugin_tools, list):
                        tools.extend(plugin_tools)
                except Exception:
                    pass

            tools.extend(self._dynamic_tools.get(plugin.id, []))

        return tools

    def register(self, tool):
        """API compatibile per plugin che registrano tool programmaticamente."""
        plugin_id = self._loading_plugin_id or "dynamic"
        self._dynamic_tools.setdefault(plugin_id, []).append(tool)

    def unregister(self, tool_name: str):
        """Rimuove un tool dinamico registrato da un plugin."""
        for plugin_id, tools in self._dynamic_tools.items():
            self._dynamic_tools[plugin_id] = [
                tool for tool in tools if getattr(tool, "name", None) != tool_name
            ]

    def list_plugins(self) -> list[dict]:
        """Lista tutti i plugin con stato."""
        result = []
        for plugin_id in self.discover():
            if plugin_id in self.loaded:
                p = self.loaded[plugin_id]
                result.append({
                    "id": p.id,
                    "name": p.manifest.name,
                    "version": p.manifest.version,
                    "status": "loaded" if p.loaded else "error",
                    "hooks": p.manifest.hooks,
                    "tools": len(p.manifest.tools),
                    "error": p.load_error,
                })
            else:
                # Non ancora caricato
                manifest_path = os.path.join(self.plugins_dir, plugin_id, "manifest.json")
                try:
                    manifest = PluginManifest.from_file(manifest_path)
                    result.append({
                        "id": manifest.id,
                        "name": manifest.name,
                        "version": manifest.version,
                        "status": "discovered",
                        "hooks": manifest.hooks,
                        "tools": len(manifest.tools),
                    })
                except Exception:
                    result.append({"id": plugin_id, "status": "error"})

        return result
