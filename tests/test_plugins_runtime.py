"""Test per scaffold plugin e runtime plugin-aware."""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.plugins import PluginManager
from core.tools import ToolRegistry, Tool
import tools.plugins as plugin_tools


def test_tool_registry_unregister_removes_tool():
    registry = ToolRegistry()
    registry.register(Tool(name="demo", description="demo"))

    assert registry.get("demo") is not None
    registry.unregister("demo")
    assert registry.get("demo") is None


def test_scaffold_plugin_creates_manifest_and_module():
    with tempfile.TemporaryDirectory() as tmp:
        original_openvurp_dir = plugin_tools._openvurp_dir
        plugin_tools._openvurp_dir = lambda: tmp
        try:
            result = plugin_tools.scaffold_plugin_handler(
                plugin_id="desktop_tools",
                tool_name="desktop_screenshot",
                description="Cattura uno screenshot desktop.",
            )
        finally:
            plugin_tools._openvurp_dir = original_openvurp_dir

        assert result.success
        assert os.path.exists(os.path.join(tmp, "plugins", "desktop_tools", "manifest.json"))
        assert os.path.exists(os.path.join(tmp, "plugins", "desktop_tools", "__init__.py"))


def test_plugin_manager_loads_scaffolded_tool():
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "repo")
        plugins_dir = os.path.join(root, "plugins")
        os.makedirs(plugins_dir, exist_ok=True)

        original_openvurp_dir = plugin_tools._openvurp_dir
        plugin_tools._openvurp_dir = lambda: root
        try:
            result = plugin_tools.scaffold_plugin_handler(
                plugin_id="desktop_tools",
                tool_name="desktop_screenshot",
                description="Cattura uno screenshot desktop.",
            )
            assert result.success
        finally:
            plugin_tools._openvurp_dir = original_openvurp_dir

        manager = PluginManager(plugins_dir)
        plugin = manager.load("desktop_tools")
        assert plugin.loaded

        tools = manager.get_tools()
        names = {tool.name for tool in tools}
        assert "desktop_screenshot" in names


if __name__ == "__main__":
    test_tool_registry_unregister_removes_tool()
    test_scaffold_plugin_creates_manifest_and_module()
    test_plugin_manager_loads_scaffolded_tool()
    print("Tutti i test plugin runtime passati!")
