"""
openvurp Core — Setup Wizard

Onboarding guidato: niente `.env` da scrivere a mano. Rileva la configurazione
mancante, chiede le poche cose necessarie (backend, modello, eventuale token
Telegram) e scrive il `.env` al posto tuo. Pensato per girare alla prima
apertura di `openvurp`, oppure on-demand con `openvurp --setup`.

La logica di lettura/scrittura del `.env` è pura e testabile; la parte
interattiva (rich) è sottile e isolata in run_wizard().
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

OPENVURP_DIR = Path(__file__).resolve().parent.parent
ENV_PATH = OPENVURP_DIR / ".env"
ENV_EXAMPLE = OPENVURP_DIR / ".env.example"

# Modello di default sensato per backend cloud (l'utente può cambiarlo).
DEFAULT_CLOUD_MODEL = {
    "openai": "gpt-4o",
    "anthropic": "claude-sonnet-4-6",
    "groq": "llama-3.3-70b-versatile",
}
CLOUD_KEY = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "groq": "GROQ_API_KEY",
}

# Backend che usano il login personale di un CLI già installato: non chiedono
# API key, ma richiedono che quel CLI sia autenticato.
SUBSCRIPTION_BACKENDS = {
    "codex": ("codex", "gpt-5.6-luna", "`codex login` (Accesso con ChatGPT)"),
    "claude_cli": ("claude", "sonnet", "`claude` e completa l'accesso"),
}
BACKENDS = ["ollama", "codex", "claude_cli", "openai", "anthropic", "groq"]
BACKEND_HELP = {
    "ollama": "modelli locali o cloud via Ollama",
    "codex": "abbonamento ChatGPT tramite Codex CLI",
    "claude_cli": "abbonamento Claude.ai tramite Claude Code",
    "openai": "API OpenAI a consumo",
    "anthropic": "API Anthropic a consumo",
    "groq": "API Groq a consumo",
}


# ── Helper puri (testabili) ──────────────────────────────────────────────

def parse_env(text: str) -> dict[str, str]:
    """Estrae le coppie KEY=value da un testo .env (ignora commenti/righe vuote)."""
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def apply_env_values(template: str, values: dict[str, str]) -> str:
    """Applica `values` al `template` preservando commenti e ordine.

    Le chiavi già presenti vengono aggiornate sul posto; quelle nuove vengono
    appese in fondo. Così il .env mantiene i commenti utili di .env.example.
    """
    seen: set[str] = set()
    out_lines: list[str] = []
    for raw in template.splitlines():
        stripped = raw.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in values:
                out_lines.append(f"{key}={values[key]}")
                seen.add(key)
                continue
        out_lines.append(raw)
    extra = [k for k in values if k not in seen]
    if extra:
        out_lines.append("")
        out_lines.append("# Aggiunto dal setup wizard")
        for key in extra:
            out_lines.append(f"{key}={values[key]}")
    return "\n".join(out_lines).rstrip("\n") + "\n"


def needs_setup() -> bool:
    """True se manca una configurazione minima utilizzabile."""
    if not ENV_PATH.exists():
        return True
    env = parse_env(ENV_PATH.read_text(encoding="utf-8"))
    backend = env.get("LLM_BACKEND", "").strip()
    model = env.get("LLM_MODEL", "").strip()
    if not backend or not model:
        return True
    if backend in SUBSCRIPTION_BACKENDS:
        return False  # l'accesso lo verifica il backend al primo turno
    keyname = CLOUD_KEY.get(backend)
    if keyname and not env.get(keyname, "").strip():
        return True
    return False


def write_env(values: dict[str, str]) -> Path:
    """Scrive il `.env` applicando i valori al template (o all'esistente)."""
    if ENV_PATH.exists():
        template = ENV_PATH.read_text(encoding="utf-8")
    elif ENV_EXAMPLE.exists():
        template = ENV_EXAMPLE.read_text(encoding="utf-8")
    else:
        template = ""
    content = apply_env_values(template, values)
    ENV_PATH.write_text(content, encoding="utf-8")
    try:
        os.chmod(ENV_PATH, 0o600)  # contiene segreti
    except OSError:
        pass
    # Rendi i valori attivi anche per il processo corrente: se config non è
    # ancora stato importato li leggerà da qui; se lo è già, valgono comunque.
    for key, value in values.items():
        if value:
            os.environ[key] = str(value)
    return ENV_PATH


def subscription_login_status(backend: str) -> tuple[bool, str]:
    """Stato del login del CLI in abbonamento, senza far esplodere il wizard."""
    try:
        from core.cli_backends import claude_login_status, codex_login_status
        binary = SUBSCRIPTION_BACKENDS[backend][0]
        if backend == "codex":
            return codex_login_status(binary)
        return claude_login_status(binary)
    except Exception as exc:
        return False, f"stato login non verificabile: {exc}"


def detect_ollama_models(base_url: str = "http://localhost:11434") -> list[str]:
    """Lista i modelli Ollama installati, via CLI con fallback su API."""
    try:
        proc = subprocess.run(
            ["ollama", "list"], capture_output=True, text=True, timeout=5
        )
        if proc.returncode == 0:
            models = []
            for raw in proc.stdout.splitlines():
                line = raw.strip()
                if not line or line.lower().startswith("name"):
                    continue
                name = line.split()[0]
                if name and name not in models:
                    models.append(name)
            if models:
                return models
    except Exception:
        pass
    try:
        import json
        import urllib.request
        with urllib.request.urlopen(f"{base_url}/api/tags", timeout=3) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return [m.get("name", "") for m in data.get("models", []) if m.get("name")]
    except Exception:
        return []


# ── Flusso interattivo ───────────────────────────────────────────────────

def run_wizard(force: bool = False) -> bool:
    """Esegue il wizard se serve (o se force). Ritorna True se configurato/ok."""
    if not force and not needs_setup():
        return True

    try:
        from rich.console import Console
        from rich.panel import Panel
        from rich.prompt import Confirm, Prompt
    except Exception:
        return _run_wizard_plain(force)

    console = Console()
    console.print(Panel.fit(
        "[bold yellow]✳ Welcome to openvurp[/bold yellow]\n"
        "Let's set up the essentials. You won't have to edit any file.",
        border_style="yellow",
    ))

    values: dict[str, str] = {}
    # Valori attuali del .env → default del wizard: premere Invio mantiene ciò
    # che c'è già (ri-lancio sicuro, es. alla rinascita dopo un reset).
    cur = parse_env(ENV_PATH.read_text(encoding="utf-8")) if ENV_PATH.exists() else {}

    for name in BACKENDS:
        console.print(f"    [dim]{name:<11}[/dim] {BACKEND_HELP[name]}")
    backend = Prompt.ask(
        "[cyan]LLM backend[/cyan]",
        choices=BACKENDS,
        default=cur.get("LLM_BACKEND") or "ollama",
    )
    values["LLM_BACKEND"] = backend

    if backend in SUBSCRIPTION_BACKENDS:
        binary, default_model, how = SUBSCRIPTION_BACKENDS[backend]
        logged_in, detail = subscription_login_status(backend)
        if logged_in:
            console.print(f"  [green]✓ {detail}[/green]")
        else:
            console.print(f"  [yellow]{detail}[/yellow] [dim]→ {how}[/dim]")
        values["LLM_MODEL"] = Prompt.ask(
            "[cyan]Model[/cyan]", default=cur.get("LLM_MODEL") or default_model,
        )
    elif backend == "ollama":
        base_url = Prompt.ask(
            "[cyan]Ollama URL[/cyan]",
            default=cur.get("LLM_BASE_URL") or "http://localhost:11434",
        )
        values["LLM_BASE_URL"] = base_url
        models = detect_ollama_models(base_url)
        if models:
            console.print("  Ollama models found:")
            for i, name in enumerate(models, 1):
                console.print(f"    [dim]{i}.[/dim] {name}")
            default_model = cur.get("LLM_MODEL") or (
                "qwen3-coder-next:cloud" if "qwen3-coder-next:cloud" in models else models[0])
            model = Prompt.ask("[cyan]Model[/cyan]", default=default_model)
        else:
            console.print(
                "  [dim]No model detected (Ollama off or empty). "
                "I'll set it anyway; pull it later with `ollama pull`.[/dim]"
            )
            model = Prompt.ask(
                "[cyan]Model[/cyan]",
                default=cur.get("LLM_MODEL") or "qwen3-coder-next:cloud",
            )
        values["LLM_MODEL"] = model
    else:
        keyname = CLOUD_KEY[backend]
        key = Prompt.ask(f"[cyan]{keyname}[/cyan]", password=True,
                         default=cur.get(keyname, ""))
        values[keyname] = key
        model = Prompt.ask(
            "[cyan]Model[/cyan]",
            default=cur.get("LLM_MODEL") or DEFAULT_CLOUD_MODEL[backend],
        )
        values["LLM_MODEL"] = model

    # Telegram qui e' solo un citofono in uscita: si chiacchiera con gli agenti
    # dalla pagina web. Serve a essere raggiunti quando non sei davanti al PC.
    if Confirm.ask("\n[cyan]Telegram notifications on your phone?[/cyan]", default=False):
        token = Prompt.ask("[cyan]Bot token[/cyan]", password=True, default="")
        if token.strip():
            values["TELEGRAM_TOKEN"] = token.strip()
            console.print(
                "  [dim]Send any message to your bot, then open "
                "api.telegram.org/bot<token>/getUpdates to read your chat id.[/dim]"
            )
            chat_id = Prompt.ask("[cyan]Chat id to notify[/cyan]", default="")
            if chat_id.strip():
                values["TELEGRAM_CHAT_ID"] = chat_id.strip()

    console.print("\n[bold]What do you want active?[/bold] [dim](change anytime with `openvurp --setup`)[/dim]")
    if Confirm.ask("[cyan]Web dashboard (chat from your browser)?[/cyan]", default=False):
        values["DASHBOARD_ENABLED"] = "true"
    if Confirm.ask("[cyan]Voice replies (text-to-speech)?[/cyan]", default=False):
        values["VOICE_ENABLED"] = "true"

    path = write_env(values)
    console.print(Panel.fit(
        f"[green]✓ Configuration saved[/green] to [dim]{path}[/dim]\n"
        "Avvio openvurp…",
        border_style="green",
    ))
    return True


def current_config() -> dict[str, str]:
    """Config effettiva del .env, per mostrarla prima di partire."""
    if not ENV_PATH.exists():
        return {}
    return parse_env(ENV_PATH.read_text(encoding="utf-8"))


def quick_engine_menu(console=None) -> bool:
    """Cambio rapido di backend/modello, senza rifare tutto il setup.

    E' la scelta che si fa piu' spesso ("oggi lo voglio su Ollama") e non deve
    costare l'intero wizard con Telegram, dashboard e voce.
    """
    try:
        from rich.prompt import Prompt
        if console is None:
            from rich.console import Console
            console = Console()
    except Exception:
        return _quick_engine_menu_plain()

    cur = current_config()
    console.print("\n  [bold]Engine[/bold]")
    for name in BACKENDS:
        mark = "[green]•[/green]" if cur.get("LLM_BACKEND") == name else " "
        console.print(f"   {mark} [cyan]{name:<11}[/cyan] [dim]{BACKEND_HELP[name]}[/dim]")
    backend = Prompt.ask("  [cyan]backend[/cyan]", choices=BACKENDS,
                         default=cur.get("LLM_BACKEND") or "ollama")
    values = {"LLM_BACKEND": backend}

    if backend in SUBSCRIPTION_BACKENDS:
        _, default_model, how = SUBSCRIPTION_BACKENDS[backend]
        logged_in, detail = subscription_login_status(backend)
        console.print(f"  [green]✓ {detail}[/green]" if logged_in
                      else f"  [yellow]{detail}[/yellow] [dim]→ {how}[/dim]")
    elif backend == "ollama":
        default_model = cur.get("LLM_MODEL") or "qwen3-coder-next:cloud"
        models = detect_ollama_models(cur.get("LLM_BASE_URL") or "http://localhost:11434")
        for name in models[:15]:
            console.print(f"    [dim]- {name}[/dim]")
    else:
        default_model = cur.get("LLM_MODEL") or DEFAULT_CLOUD_MODEL[backend]
        keyname = CLOUD_KEY[backend]
        if not cur.get(keyname, "").strip():
            key = Prompt.ask(f"  [cyan]{keyname}[/cyan]", password=True, default="")
            if key.strip():
                values[keyname] = key.strip()

    values["LLM_MODEL"] = Prompt.ask("  [cyan]modello[/cyan]",
                                     default=cur.get("LLM_MODEL") or default_model)
    write_env(values)
    console.print(f"  [green]✓[/green] {values['LLM_BACKEND']} / {values['LLM_MODEL']}")
    return True


def _quick_engine_menu_plain() -> bool:
    cur = current_config()
    backend = (input(f"backend [{'/'.join(BACKENDS)}] ({cur.get('LLM_BACKEND', 'ollama')}): ").strip()
               or cur.get("LLM_BACKEND") or "ollama")
    if backend in SUBSCRIPTION_BACKENDS:
        default_model = SUBSCRIPTION_BACKENDS[backend][1]
    elif backend == "ollama":
        default_model = "qwen3-coder-next:cloud"
    else:
        default_model = DEFAULT_CLOUD_MODEL.get(backend, "")
    model = input(f"modello ({cur.get('LLM_MODEL') or default_model}): ").strip() \
        or cur.get("LLM_MODEL") or default_model
    write_env({"LLM_BACKEND": backend, "LLM_MODEL": model})
    print(f"✓ {backend} / {model}")
    return True


def _run_wizard_plain(force: bool) -> bool:
    """Fallback senza rich (input() puro)."""
    print("✳ openvurp setup — no files to edit by hand.")
    backend = (input(f"Backend [{'/'.join(BACKENDS)}] (ollama): ").strip()
               or "ollama")
    values: dict[str, str] = {"LLM_BACKEND": backend}
    if backend in SUBSCRIPTION_BACKENDS:
        _, default_model, how = SUBSCRIPTION_BACKENDS[backend]
        logged_in, detail = subscription_login_status(backend)
        print(("✓ " if logged_in else "! ") + detail + ("" if logged_in else f" → {how}"))
        values["LLM_MODEL"] = (input(f"Model ({default_model}): ").strip()
                               or default_model)
    elif backend == "ollama":
        base_url = (input("Ollama URL (http://localhost:11434): ").strip()
                    or "http://localhost:11434")
        values["LLM_BASE_URL"] = base_url
        values["LLM_MODEL"] = (input("Model (qwen3-coder-next:cloud): ").strip()
                               or "qwen3-coder-next:cloud")
    else:
        keyname = CLOUD_KEY[backend]
        values[keyname] = input(f"{keyname}: ").strip()
        values["LLM_MODEL"] = (input(f"Model ({DEFAULT_CLOUD_MODEL[backend]}): ").strip()
                               or DEFAULT_CLOUD_MODEL[backend])
    token = input("Telegram token for notifications (Enter to skip): ").strip()
    if token:
        values["TELEGRAM_TOKEN"] = token
        chat_id = input("Chat id to notify (Enter to skip): ").strip()
        if chat_id:
            values["TELEGRAM_CHAT_ID"] = chat_id
    if input("Web dashboard? [y/N]: ").strip().lower() in ("y", "yes", "s", "si"):
        values["DASHBOARD_ENABLED"] = "true"
    if input("Voice replies (TTS)? [y/N]: ").strip().lower() in ("y", "yes", "s", "si"):
        values["VOICE_ENABLED"] = "true"
    path = write_env(values)
    print(f"✓ Saved to {path}")
    return True


if __name__ == "__main__":
    run_wizard(force=True)
