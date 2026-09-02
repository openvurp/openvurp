"""
openvurp CLI — Interfaccia a riga di comando professionale.

Comandi:
    openvurp-cli              Avvia in modalità interattiva
    openvurp-cli chat "..."   Prompt singolo
    openvurp-cli start        Start with every service (dashboard, gateway)
    openvurp-cli status       Mostra stato agente
    openvurp-cli gateway      Avvia il runtime gateway standalone
    openvurp-cli security     Gestione sicurezza (vault, audit, integrity)
    openvurp-cli doctor       Diagnostica sistema
"""

from __future__ import annotations

import sys
import os
import argparse

# Fix encoding Windows
if sys.platform == "win32":
    os.system("")
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

OPENVURP_DIR = os.path.dirname(os.path.abspath(__file__))


def main():
    parser = argparse.ArgumentParser(
        prog="openvurp-cli",
        description="openvurp — il polpo agente",
    )
    sub = parser.add_subparsers(dest="command")

    # openvurp-cli (nessun sottocomando) → avvia main.py
    # openvurp-cli chat "prompt"
    chat_p = sub.add_parser("chat", help="Prompt singolo")
    chat_p.add_argument("prompt", nargs="+", help="Il prompt")
    chat_p.add_argument("--model", "-m", help="Override modello")
    chat_p.add_argument("--backend", "-b", help="Override backend")

    # openvurp-cli start
    start_p = sub.add_parser("start", help="Start full interactive mode")
    start_p.add_argument("--model", "-m", help="Override modello")
    start_p.add_argument("--backend", "-b", help="Override backend")
    start_p.add_argument("--dashboard", action="store_true")
    start_p.add_argument("--gateway", action="store_true")

    # openvurp-cli status
    sub.add_parser("status", help="System status")
    sub.add_parser("gateway", help="Start standalone runtime gateway")

    # openvurp-cli security
    sec_p = sub.add_parser("security", help="Gestione sicurezza")
    sec_sub = sec_p.add_subparsers(dest="sec_command")

    # openvurp-cli security vault
    vault_p = sec_sub.add_parser("vault", help="Gestione secrets vault")
    vault_p.add_argument("action", choices=["init", "list", "set", "get", "delete"],
                         help="Azione vault")
    vault_p.add_argument("key", nargs="?", help="Nome del secret")
    vault_p.add_argument("value", nargs="?", help="Valore del secret (per set)")
    vault_p.add_argument("--password", "-p", help="Master password")

    # openvurp-cli security audit
    audit_p = sec_sub.add_parser("audit", help="Audit log")
    audit_p.add_argument("action", choices=["show", "verify", "failures"],
                         help="Azione audit")
    audit_p.add_argument("-n", type=int, default=20, help="Numero eventi")

    # openvurp-cli security integrity
    integ_p = sec_sub.add_parser("integrity", help="Verify file integrity")
    integ_p.add_argument("action", choices=["baseline", "verify", "check"],
                         help="Azione integrity")
    integ_p.add_argument("file", nargs="?", help="File specifico (per check)")

    # openvurp-cli security rbac
    rbac_p = sec_sub.add_parser("rbac", help="Gestione ruoli e permessi")
    rbac_p.add_argument("action", choices=["list", "set", "remove"],
                        help="Azione RBAC")
    rbac_p.add_argument("user_id", nargs="?", help="ID utente")
    rbac_p.add_argument("--role", choices=["admin", "power", "user", "reader", "guest"],
                        help="Ruolo")

    # openvurp-cli doctor
    sub.add_parser("doctor", help="Diagnostica sistema")

    args = parser.parse_args()

    if args.command is None:
        # Nessun sottocomando → avvia interattivo
        _run_interactive(args)
    elif args.command == "chat":
        _run_chat(args)
    elif args.command == "start":
        _run_interactive(args)
    elif args.command == "status":
        _run_status()
    elif args.command == "gateway":
        _run_gateway()
    elif args.command == "security":
        _run_security(args)
    elif args.command == "doctor":
        _run_doctor()
    else:
        parser.print_help()


def _run_interactive(args):
    """Avvia openvurp in modalità interattiva (delega a main.py)."""
    sys.argv = ["main.py"]
    if hasattr(args, "model") and args.model:
        sys.argv.extend(["--model", args.model])
    if hasattr(args, "backend") and args.backend:
        sys.argv.extend(["--backend", args.backend])
    if hasattr(args, "dashboard") and args.dashboard:
        sys.argv.append("--dashboard")
    if hasattr(args, "gateway") and args.gateway:
        sys.argv.append("--gateway")
    from main import main as main_entry
    main_entry()


def _run_chat(args):
    """Prompt singolo."""
    sys.argv = ["main.py"] + args.prompt
    if args.model:
        sys.argv.extend(["--model", args.model])
    if args.backend:
        sys.argv.extend(["--backend", args.backend])
    from main import main as main_entry
    main_entry()


def _run_status():
    """Mostra stato del sistema."""
    from rich.console import Console
    from rich.table import Table
    from rich import box

    console = Console()
    console.print("\n[bold cyan]openvurp Status[/bold cyan]\n")

    table = Table(box=box.ROUNDED, show_header=False, padding=(0, 2))
    table.add_column("Key", style="dim")
    table.add_column("Value")

    # Config
    try:
        import config
        table.add_row("Backend", getattr(config, "LLM_BACKEND", "?"))
        table.add_row("Modello", getattr(config, "LLM_MODEL", "?"))
        table.add_row("Max Iterations", str(getattr(config, "MAX_ITERATIONS", "?")))
        table.add_row("Context Max", str(getattr(config, "CONTEXT_MAX_TOKENS", "?")))
        gateway_enabled = bool(getattr(config, "GATEWAY_ENABLED", False))
        gateway_port = getattr(config, "GATEWAY_PORT", 8421)
        table.add_row("Gateway", f"[green]http://127.0.0.1:{gateway_port}[/green]" if gateway_enabled else "[dim]Disabilitato[/dim]")
    except Exception:
        table.add_row("Config", "[red]Load error[/red]")

    # Telegram: solo notifiche in uscita
    telegram_token = os.environ.get("TELEGRAM_TOKEN", "")
    try:
        import config
        telegram_token = telegram_token or getattr(config, "TELEGRAM_TOKEN", "")
    except Exception:
        pass
    table.add_row("Telegram notifications",
                  "[green]Configured[/green]" if telegram_token else "[dim]Not configured[/dim]")

    # Heartbeat
    heartbeat_path = os.path.join(OPENVURP_DIR, "heartbeat.json")
    heartbeat_md = os.path.join(OPENVURP_DIR, "HEARTBEAT.md")
    if os.path.exists(heartbeat_path):
        try:
            from core.heartbeat import load_heartbeat_config
            hb_cfg = load_heartbeat_config(OPENVURP_DIR)
            hb_status = f"[green]Ogni {hb_cfg.interval_seconds // 60}min[/green]" if hb_cfg.enabled else "[dim]Disabilitato[/dim]"
            table.add_row("Heartbeat", hb_status)
        except Exception:
            table.add_row("Heartbeat", "[dim]Errore config[/dim]")
    else:
        table.add_row("Heartbeat", "[dim]Non configurato[/dim]")
    table.add_row("HEARTBEAT.md", "[green]Presente[/green]" if os.path.exists(heartbeat_md) else "[dim]Non presente[/dim]")

    # Security
    vault_path = os.path.join(OPENVURP_DIR, "memory", "vault", "secrets.vault")
    table.add_row("Vault", "[green]Inizializzato[/green]" if os.path.exists(vault_path) else "[dim]Non inizializzato[/dim]")

    audit_path = os.path.join(OPENVURP_DIR, "memory", "audit", "audit.jsonl")
    if os.path.exists(audit_path):
        size = os.path.getsize(audit_path)
        table.add_row("Audit Log", f"[green]{size // 1024}KB[/green]")
    else:
        table.add_row("Audit Log", "[dim]Vuoto[/dim]")

    baseline_path = os.path.join(OPENVURP_DIR, ".integrity_baseline.json")
    table.add_row("Integrity", "[green]Baseline presente[/green]" if os.path.exists(baseline_path) else "[dim]Nessun baseline[/dim]")

    # Dipendenze opzionali
    deps = {
        "cryptography": "Vault encryption",
        "openai": "OpenAI backend",
        "anthropic": "Anthropic backend",
        "groq": "Groq backend",
        "playwright": "Browser automation",
        "whisper": "Audio transcription",
    }
    for mod, desc in deps.items():
        try:
            __import__(mod)
            table.add_row(desc, "[green]OK[/green]")
        except ImportError:
            table.add_row(desc, "[dim]Non installato[/dim]")

    console.print(table)
    console.print()


def _run_gateway():
    from runtime_gateway import main as gateway_main
    gateway_main()


def _run_security(args):
    """Gestione sicurezza."""
    from rich.console import Console
    console = Console()

    if not args.sec_command:
        console.print("[yellow]Uso: openvurp-cli security {vault|audit|integrity|rbac}[/yellow]")
        return

    if args.sec_command == "vault":
        _security_vault(args, console)
    elif args.sec_command == "audit":
        _security_audit(args, console)
    elif args.sec_command == "integrity":
        _security_integrity(args, console)
    elif args.sec_command == "rbac":
        _security_rbac(args, console)


def _security_vault(args, console):
    """Gestione vault."""
    from core.security.vault import Vault
    vault_dir = os.path.join(OPENVURP_DIR, "memory", "vault")
    vault = Vault(vault_dir)

    if args.action == "init":
        msg = vault.init(args.password)
        console.print(f"[green]{msg}[/green]")

    elif args.action == "list":
        vault.auto_unlock()
        keys = vault.list_keys()
        if keys:
            for k in keys:
                console.print(f"  - {k}")
        else:
            console.print("[dim]Vault vuoto.[/dim]")

    elif args.action == "set":
        if not args.key or not args.value:
            console.print("[red]Uso: openvurp-cli security vault set KEY VALUE[/red]")
            return
        vault.auto_unlock()
        vault.set(args.key, args.value)
        console.print(f"[green]Secret '{args.key}' saved.[/green]")

    elif args.action == "get":
        if not args.key:
            console.print("[red]Uso: openvurp-cli security vault get KEY[/red]")
            return
        vault.auto_unlock()
        val = vault.get(args.key)
        if val:
            console.print(f"{args.key} = {val}")
        else:
            console.print(f"[dim]'{args.key}' non trovato.[/dim]")

    elif args.action == "delete":
        if not args.key:
            console.print("[red]Uso: openvurp-cli security vault delete KEY[/red]")
            return
        vault.auto_unlock()
        if vault.delete(args.key):
            console.print(f"[green]Secret '{args.key}' eliminato.[/green]")
        else:
            console.print(f"[dim]'{args.key}' non trovato.[/dim]")


def _security_audit(args, console):
    """Audit log."""
    from core.security.audit import AuditLog
    audit_dir = os.path.join(OPENVURP_DIR, "memory", "audit")
    audit = AuditLog(audit_dir)

    if args.action == "show":
        events = audit.get_recent(args.n)
        if not events:
            console.print("[dim]No events.[/dim]")
            return

        from rich.table import Table
        from rich import box
        import time as _time

        table = Table(box=box.SIMPLE, show_lines=False)
        table.add_column("Tempo", style="dim", width=19)
        table.add_column("Azione", width=16)
        table.add_column("Target", max_width=40)
        table.add_column("Rischio", width=8)
        table.add_column("OK", width=3)

        for e in events:
            ts = _time.strftime("%Y-%m-%d %H:%M:%S", _time.localtime(e["timestamp"]))
            risk_style = {"low": "green", "medium": "yellow", "high": "red", "critical": "bold red"}.get(e.get("risk_level", "low"), "white")
            ok = "[green]✓[/green]" if e.get("success") else "[red]✗[/red]"
            table.add_row(ts, e.get("action", "?"), e.get("target", "?")[:40], f"[{risk_style}]{e.get('risk_level', '?')}[/{risk_style}]", ok)

        console.print(table)

    elif args.action == "verify":
        valid, count, msg = audit.verify_chain()
        style = "green" if valid else "red"
        console.print(f"[{style}]{msg}[/{style}]")

    elif args.action == "failures":
        events = audit.get_failures(args.n)
        if not events:
            console.print("[green]No failures recorded.[/green]")
            return
        for e in events:
            console.print(f"  [{e.get('action')}] {e.get('target', '?')} — {e.get('details', '')[:80]}")


def _security_integrity(args, console):
    """Verifica integrità."""
    from core.security.integrity import IntegrityChecker
    checker = IntegrityChecker(OPENVURP_DIR)

    if args.action == "baseline":
        n = checker.create_baseline()
        console.print(f"[green]Baseline creato: {n} file registrati.[/green]")

    elif args.action == "verify":
        report = checker.verify()
        if report.valid:
            console.print(f"[green]{report.message}[/green]")
        else:
            console.print(f"[red]{report.message}[/red]")
            if report.modified:
                console.print(f"  File modificati: {', '.join(report.modified)}")
            if report.missing:
                console.print(f"  File mancanti: {', '.join(report.missing)}")
        if report.new_files:
            console.print(f"  [yellow]File nuovi: {', '.join(report.new_files)}[/yellow]")

    elif args.action == "check":
        if not args.file:
            console.print("[red]Uso: openvurp-cli security integrity check FILE[/red]")
            return
        valid, msg = checker.verify_file(args.file)
        style = "green" if valid else "red"
        console.print(f"[{style}]{msg}[/{style}]")


def _security_rbac(args, console):
    """Gestione RBAC."""
    from core.security.rbac import RBAC, Role
    config_dir = os.path.join(OPENVURP_DIR, "memory")
    rbac = RBAC(config_dir)

    if args.action == "list":
        users = rbac.list_users()
        if not users:
            console.print("[dim]No users configured (CLI owner is always admin).[/dim]")
            return
        from rich.table import Table
        from rich import box
        table = Table(box=box.ROUNDED)
        table.add_column("User ID")
        table.add_column("Ruolo")
        table.add_column("Canali")
        for u in users:
            table.add_row(u["user_id"], u["role"], ", ".join(u["channels"]) or "tutti")
        console.print(table)

    elif args.action == "set":
        if not args.user_id or not args.role:
            console.print("[red]Uso: openvurp-cli security rbac set USER_ID --role ROLE[/red]")
            return
        role = Role(args.role)
        rbac.set_user(args.user_id, role)
        console.print(f"[green]{args.user_id} → {role.value}[/green]")

    elif args.action == "remove":
        if not args.user_id:
            console.print("[red]Uso: openvurp-cli security rbac remove USER_ID[/red]")
            return
        if rbac.remove_user(args.user_id):
            console.print(f"[green]{args.user_id} removed (back to guest).[/green]")
        else:
            console.print(f"[dim]{args.user_id} non trovato.[/dim]")


def _run_doctor():
    """Diagnostica sistema."""
    from rich.console import Console
    console = Console()
    console.print("\n[bold cyan]openvurp Doctor[/bold cyan]\n")

    checks = []

    # Python version
    ver = sys.version_info
    ok = ver >= (3, 10)
    checks.append(("Python >= 3.10", ok, f"{ver.major}.{ver.minor}.{ver.micro}"))

    # Core modules
    core_modules = [
        "core.agent", "core.llm", "core.tools", "core.executor",
        "core.safety", "core.memory", "core.context", "core.session",
        "core.security",
    ]
    for mod in core_modules:
        try:
            __import__(mod)
            checks.append((mod, True, "OK"))
        except Exception as e:
            checks.append((mod, False, str(e)[:60]))

    # External deps
    ext_deps = [
        ("requests", "HTTP client"),
        ("httpx", "Async HTTP"),
        ("rich", "Terminal UI"),
    ]
    for mod, desc in ext_deps:
        try:
            __import__(mod)
            checks.append((f"{desc} ({mod})", True, "OK"))
        except ImportError:
            checks.append((f"{desc} ({mod})", False, "pip install " + mod))

    # Optional deps
    opt_deps = [
        ("cryptography", "Vault encryption"),
        ("openai", "OpenAI backend"),
        ("anthropic", "Anthropic backend"),
        ("groq", "Groq backend"),
        ("whisper", "Audio (Whisper)"),
        ("playwright", "Browser automation"),
        ("telegram", "Telegram (notifications)"),
    ]
    for mod, desc in opt_deps:
        try:
            __import__(mod)
            checks.append((f"[opzionale] {desc}", True, "OK"))
        except ImportError:
            checks.append((f"[opzionale] {desc}", None, "Non installato"))

    # Docker
    import subprocess
    try:
        r = subprocess.run(["docker", "info"], capture_output=True, timeout=5)
        checks.append(("[opzionale] Docker", r.returncode == 0, "Disponibile" if r.returncode == 0 else "Non raggiungibile"))
    except Exception:
        checks.append(("[opzionale] Docker", None, "Non installato"))

    # npx chrome-devtools-mcp
    try:
        r = subprocess.run(["npx", "--version"], capture_output=True, timeout=5, text=True)
        checks.append(("[opzionale] npx (Chrome DevTools MCP)", r.returncode == 0, f"v{r.stdout.strip()}" if r.returncode == 0 else "Errore"))
    except Exception:
        checks.append(("[opzionale] npx (Chrome DevTools MCP)", None, "Non installato"))

    # Print
    for name, ok, detail in checks:
        if ok is True:
            console.print(f"  [green]✓[/green] {name}: {detail}")
        elif ok is False:
            console.print(f"  [red]✗[/red] {name}: {detail}")
        else:
            console.print(f"  [dim]○[/dim] {name}: {detail}")

    console.print()


if __name__ == "__main__":
    main()
