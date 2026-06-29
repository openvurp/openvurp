"""
openvurp Core — Doctor

Diagnostica rapida del runtime, del workspace e dei controlli di sicurezza.
"""

from __future__ import annotations

import os
import urllib.request
from dataclasses import dataclass, field

import config as cfg

from core.browser_manager import get_browser_manager
from core.environment import EnvironmentInspector
from core.model_router import choose_small_local_ollama_model
from core.plugins import PluginManager
from core.runtime_api import collect_runtime_overview
from core.runtime_shell import resolve_effective_shell
from core.security.audit import AuditLog
from core.security.integrity import IntegrityChecker
from core.setup_runtime import ensure_runtime_state, SetupReport


@dataclass
class DoctorReport:
    ok: bool
    warnings: list[str] = field(default_factory=list)
    sections: list[tuple[str, list[str]]] = field(default_factory=list)

    def render(self) -> str:
        status = "OK" if self.ok else "ATTENZIONE"
        lines = [f"## DOCTOR [{status}]"]
        if self.warnings:
            lines.append("Warning:")
            for warning in self.warnings:
                lines.append(f"- {warning}")
        for title, entries in self.sections:
            lines.append(f"\n### {title}")
            lines.extend(entries)
        return "\n".join(lines)


def fix_runtime_issues(workspace_dir: str, allowed_telegram_users: list[int] | None = None) -> SetupReport:
    return ensure_runtime_state(
        workspace_dir,
        allowed_telegram_users=allowed_telegram_users,
        create_integrity_baseline=True,
        force_acl_refresh=bool(allowed_telegram_users),
    )


def build_doctor_report(workspace_dir: str, tool_names: list[str]) -> DoctorReport:
    warnings: list[str] = []
    sections: list[tuple[str, list[str]]] = []

    env = EnvironmentInspector(workspace_dir).get_snapshot(force=True)
    configured_shell = str(getattr(cfg, "SHELL", "") or "").strip()
    resolved_shell = resolve_effective_shell(configured_shell)
    if configured_shell and resolved_shell.source != "configured":
        warnings.append(
            "The configured SHELL is invalid on the current runtime: it is ignored and replaced with the detected actual shell."
        )
    sections.append((
        "Runtime",
        [
            f"- backend: `{getattr(cfg, 'LLM_BACKEND', '?')}`",
            f"- model: `{getattr(cfg, 'LLM_MODEL', '?')}`",
            f"- shell configurata: `{configured_shell or 'auto'}`",
            f"- shell effettiva: `{env.shell_name}` ({env.shell_family})",
            f"- path shell effettivo: `{env.shell_path or resolved_shell.path or '?'}`",
            f"- sandbox mode: `{getattr(cfg, 'SANDBOX_MODE', 'restricted')}`",
            f"- workspace: `{workspace_dir}`",
        ],
    ))

    workspace_files = (
        "AGENTS.md",
        "SOUL.md",
        "USER.md",
        "IDENTITY.md",
        "MEMORY.md",
        "TOOLS.md",
        "HEARTBEAT.md",
    )
    present = []
    missing = []
    for name in workspace_files:
        if os.path.exists(os.path.join(workspace_dir, name)):
            present.append(name)
        else:
            missing.append(name)

    if missing:
        warnings.append(f"Missing workspace files: {', '.join(missing)}")
    sections.append((
        "Workspace",
        [
            f"- file presenti: {', '.join(present) if present else 'nessuno'}",
            f"- missing files: {', '.join(missing) if missing else 'none'}",
            f"- progetto rilevato: {', '.join(env.project_types) if env.project_types else 'nessun marker forte'}",
            f"- branch git: {env.git_branch or 'non disponibile'}",
        ],
    ))

    integrity_checker = IntegrityChecker(workspace_dir)
    integrity = integrity_checker.verify()
    audit_dir = os.path.join(workspace_dir, "memory", "audit")
    audit_valid, audit_count, audit_msg = AuditLog(audit_dir).verify_chain()
    integrity_baseline_exists = os.path.exists(
        os.path.join(workspace_dir, IntegrityChecker.BASELINE_FILE)
    )
    integrity_message = integrity.message
    if not integrity_baseline_exists:
        integrity_message = "Nessun baseline integrity ancora creato."
        warnings.append("Integrity baseline missing: run doctor_fix to initialize it.")
    elif not integrity.valid:
        warnings.append(integrity.message)
    if not audit_valid:
        warnings.append(audit_msg)
    acl_exists = os.path.exists(os.path.join(workspace_dir, 'memory', 'acl.json'))
    if not acl_exists:
        warnings.append("ACL missing: run doctor_fix or runtime setup.")
    sections.append((
        "Security",
        [
            f"- integrity: {integrity_message}",
            f"- audit chain: {audit_msg}",
            f"- acl: {'present' if acl_exists else 'missing'}",
            f"- eventi audit recenti: {audit_count}",
        ],
    ))

    plugin_mgr = PluginManager(os.path.join(workspace_dir, "plugins"))
    discovered = plugin_mgr.discover()
    loaded = 0
    failed: list[str] = []
    disabled = 0
    for plugin_id in discovered:
        plugin = plugin_mgr.load(plugin_id)
        if plugin.loaded:
            loaded += 1
        elif plugin.load_error == "Plugin disabilitato":
            disabled += 1
        else:
            failed.append(f"{plugin_id}: {plugin.load_error or 'unknown error'}")
    if failed:
        warnings.append(f"Plugins not loaded: {', '.join(failed)}")
    sections.append((
        "Plugin",
        [
            f"- scoperti: {len(discovered)}",
            f"- caricati: {loaded}",
            f"- disabilitati: {disabled}",
            f"- errori: {', '.join(failed) if failed else 'nessuno'}",
        ],
    ))

    capabilities = []
    critical_capabilities = {"process_start", "subagent_spawn", "doctor"}
    for name in (
        "process_start",
        "process_read",
        "notify",
        "image_analyze",
        "audio_transcribe",
        "pdf_read",
        "scaffold_plugin",
        "reload_plugins",
        "request_restart",
        "desktop_screenshot",
        "notify_file",
        "notify_photo",
        "memory_consolidate",
        "learning_feedback",
        "learning_review",
        "learning_promote",
        "task_journal",
        "reflection_note",
        "open_loop",
        "agent_state",
        "capability_lease",
        "subagent_spawn",
        "subagent_wait",
        "subagent_wait_all",
        "doctor",
        "doctor_fix",
    ):
        capabilities.append(f"- {name}: {'yes' if name in tool_names else 'no'}")
        if tool_names and name in critical_capabilities and name not in tool_names:
            warnings.append(f"Critical capability missing: {name}")
    sections.append(("Capability", capabilities))

    browser_entries = [
        f"- tool browser: {'si' if 'browser' in tool_names else 'no'}",
        f"- tool browser_devtools: {'si' if 'browser_devtools' in tool_names else 'no'}",
        f"- tool browser_setup: {'si' if 'browser_setup' in tool_names else 'no'}",
    ]
    try:
        browser_status = get_browser_manager().status().splitlines()
        browser_entries.extend(f"- {line}" for line in browser_status)
        if 'browser' in tool_names:
            if any("playwright: missing" in line for line in browser_status):
                warnings.append("Browser tool present but Playwright is not installed.")
            if any("playwright_driver: not ready" in line for line in browser_status):
                warnings.append("Browser isolated not ready: the Playwright driver fails preflight.")
            if any("shared_mode: not ready" in line for line in browser_status):
                warnings.append("Browser shared not ready: Chrome/Edge/Brave with remote debugging unreachable.")
            if any("default_engine:" in line for line in browser_status) and any("isolated_mode: not ready" in line for line in browser_status):
                warnings.append("Browser isolated not ready for the default engine.")
    except Exception as exc:
        browser_entries.append(f"- browser doctor error: {exc}")
        warnings.append(f"Browser doctor failed: {exc}")
    sections.append(("Browser", browser_entries))

    router_entries = [
        f"- subagent runtime: `{getattr(cfg, 'SUBAGENT_RUNTIME', 'process')}`",
        f"- subagent mode default: `{getattr(cfg, 'SUBAGENT_DEFAULT_MODE', 'auto')}`",
        f"- subagent max concurrent: `{getattr(cfg, 'SUBAGENT_MAX_CONCURRENT', 4)}`",
        f"- subagent timeout: `{getattr(cfg, 'SUBAGENT_TIMEOUT_SECONDS', 180)}s`",
    ]
    local_small = choose_small_local_ollama_model()
    if local_small:
        router_entries.append(f"- small local worker detected: `{local_small}`")
    else:
        router_entries.append("- small local worker detected: none")
        if getattr(cfg, "LLM_MODEL", "").lower().find("cloud") >= 0:
            warnings.append("Cloud parent active but no small local model detected for subagents.")
    sections.append(("Subagent Routing", router_entries))

    gateway_info = collect_runtime_overview(workspace_dir)
    gateway_entries = [
        f"- enabled: {'si' if gateway_info['gateway_enabled'] else 'no'}",
        f"- host: `{gateway_info['gateway_host']}`",
        f"- port: `{gateway_info['gateway_port']}`",
        f"- event log: `{gateway_info['event_log']}`",
        f"- session snapshot: {gateway_info['sessions']}",
    ]
    if gateway_info["gateway_enabled"]:
        try:
            with urllib.request.urlopen(
                f"http://{gateway_info['gateway_host']}:{gateway_info['gateway_port']}/health",
                timeout=1.5,
            ) as response:
                payload = response.read().decode("utf-8", errors="replace")
            gateway_entries.append(f"- health: {payload[:120]}")
        except Exception:
            warnings.append("Gateway enabled but health endpoint unreachable.")
            gateway_entries.append("- health: unreachable")
    sections.append(("Gateway", gateway_entries))

    return DoctorReport(ok=not warnings, warnings=warnings, sections=sections)
