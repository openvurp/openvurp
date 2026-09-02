"""
openvurp 4.0 — Entry point.

Integra core/ per tool system, reasoning, safety, observability.
"""

import sys
import os
import json
import time
import argparse
import threading

from core.personality import format_callback_response, parse_response_directive
from core.capabilities import inspect_runtime_capabilities, render_capability_prompt
from core.setup_runtime import ensure_runtime_state

if sys.platform == "win32":
    os.system("")
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def parse_args():
    parser = argparse.ArgumentParser(description="openvurp — the octopus agent")
    parser.add_argument("prompt", nargs="*", help="Direct prompt from the command line")
    parser.add_argument("--model", "-m", help="Override LLM model")
    parser.add_argument("--backend", "-b", help="Override backend (ollama/groq/openai/anthropic)")
    parser.add_argument("--dashboard", action="store_true", help="Start web dashboard")
    parser.add_argument("--gateway", action="store_true", help="Start local HTTP runtime gateway")
    parser.add_argument("--doctor", action="store_true", help="Print runtime diagnostics and exit")
    parser.add_argument("--doctor-fix", action="store_true", help="Apply full runtime bootstrap, then exit")
    parser.add_argument("--headless", action="store_true",
                        help="Start services (dashboard/gateway/heartbeat) without interactive loop — for Docker/server")
    parser.add_argument("--setup", action="store_true",
                        help="Run guided setup (backend/model/notifications), then start")
    parser.add_argument("--menu", action="store_true",
                        help="Always show the startup menu, even if disabled in .env")
    parser.add_argument("--no-menu", action="store_true",
                        help="Skip the startup menu")
    parser.add_argument("--no-browser", action="store_true",
                        help="Do not open the browser automatically")
    return parser.parse_args()


# Lock per accesso thread-safe all'agent
_agent_lock = threading.Lock()

# In modalita' web il terminale serve solo a leggere l'indirizzo: i servizi
# si riassumono in una riga invece di annunciarsi uno per uno.
QUIET_STARTUP = False


def finalize_channel_response(text: str, source: str) -> str:
    """Normalizza una risposta di callback mantenendo i control token utili."""
    return format_callback_response(text, source=source)


def render_swarm_command(text, agent) -> str:
    """Comandi dello sciame come testo, per i canali senza console.

    Stessa semantica della CLI: a uno (`@nome` o `/swarm ask`), a tutti
    (`/swarm all`) o fra loro (`/swarm discuss`).
    """
    swarm = getattr(agent, "swarm", None)
    if swarm is None:
        return "Sciame non attivo (SWARM_ENABLED=false)."

    from core.swarm import Swarm, SwarmError

    raw = (text or "").strip()
    if raw.startswith("@") and " " in raw:
        target, message = raw[1:].split(" ", 1)
        raw = f"/swarm ask {target} {message}"

    parts = raw.split(maxsplit=2)
    sub = parts[1].lower() if len(parts) > 1 else ""
    rest = parts[2] if len(parts) > 2 else ""

    try:
        if not sub or sub in ("list", "ls"):
            return swarm.roster_text() + "\n\n" + SWARM_HELP
        if sub in ("help", "?"):
            return SWARM_HELP
        if sub in ("new", "spawn"):
            name, _, role = rest.partition(" ")
            if not name or not role.strip():
                return "Uso: /swarm new <nome> <ruolo>"
            return "Convocato: " + swarm.spawn(
                name, role.strip()).describe()
        if sub in ("bye", "dismiss", "kill"):
            return "Congedato: " + swarm.dismiss(rest.strip())
        if sub == "log":
            entries = swarm.transcript(int(rest) if rest.strip().isdigit() else 20)
            if not entries:
                return "Nessuno scambio registrato."
            return "\n".join(
                f"{e.get('at', '')[11:19]} {e.get('from')} → {e.get('to')}: "
                f"{str(e.get('text', ''))[:300]}" for e in entries
            )
        if sub == "ask":
            name, _, message = rest.partition(" ")
            if not name or not message.strip():
                return "Uso: /swarm ask <nome> <messaggio>"
            return f"{name}: " + swarm.ask(name, message.strip(), sender="utente")
        if sub == "all":
            if not rest.strip():
                return "Uso: /swarm all <messaggio>"
            replies = swarm.broadcast(rest.strip(), sender="utente")
            return "\n\n".join(f"{who}: {txt}" for who, txt in replies.items())
        if sub in ("discuss", "discussione"):
            rounds, topic = 2, rest.strip()
            first, _, tail = topic.partition(" ")
            if first.isdigit() and tail.strip():
                rounds, topic = int(first), tail.strip()
            if not topic:
                return "Uso: /swarm discuss [giri] <argomento>"
            return Swarm.render_discussion(
                swarm.discuss(topic, rounds=rounds, sender="utente")
            )
        return f"Sottocomando sconosciuto: {sub}\n\n" + SWARM_HELP
    except SwarmError as exc:
        return str(exc)
    except Exception as exc:
        return f"Errore sciame: {exc}"


def render_command(text, agent, openvurp_dir, memory_dir):
    """Rende l'output di un comando-pannello come testo (per chi non ha una console).

    Stessi render della CLI → ogni canale vede lo stesso pannello reale.
    Ritorna None se non è un comando-pannello riconosciuto.
    """
    t = (text or "").strip().lower()
    parts = (text or "").split()

    def _num(default):
        for p in parts[1:]:
            if p.isdigit():
                return int(p)
        return default

    try:
        if t.startswith("/patti") or t.startswith("/pacts"):
            return agent.pacts.render_status() if getattr(agent, "pacts", None) else "Pacts not available."
        if t.startswith("/specchio") or t.startswith("/mirror"):
            from core.mirror import Mirror
            m = Mirror(memory_dir)
            m.harvest()
            return m.render_status()
        if t.startswith("/fucina") or t.startswith("/forge"):
            return agent.forge.render_status() if getattr(agent, "forge", None) else "Forge not available."
        if t.startswith("/swarm") or (text or "").startswith("@"):
            return render_swarm_command(text, agent)
        if t.startswith("/integrity"):
            from core.security.integrity import IntegrityChecker
            return IntegrityChecker(openvurp_dir).verify().message
        if t.startswith("/doctor"):
            from core.doctor import build_doctor_report
            return build_doctor_report(openvurp_dir, agent.tools.names()).render()
        if t.startswith("/memory"):
            lines = ["Memory files:"]
            for f in sorted(os.listdir(memory_dir)):
                p = os.path.join(memory_dir, f)
                if os.path.isfile(p):
                    lines.append(f"  {f} ({os.path.getsize(p)} B)")
            return "\n".join(lines) if len(lines) > 1 else "Memory is empty."
        if t.startswith("/skills"):
            import glob
            sk = sorted(os.path.basename(p) for p in
                        glob.glob(os.path.join(openvurp_dir, "skills", "*.md"))
                        + glob.glob(os.path.join(openvurp_dir, "skills", "*.py")))
            return "Skills:\n" + "\n".join("  " + s for s in sk) if sk else "No skills."
    except Exception as e:
        return f"[error: {e}]"
    return None


def check_restarted(openvurp_dir: str) -> str:
    """Se .restarted esiste, lo consuma e ritorna il motivo. Altrimenti ''."""
    restarted_path = os.path.join(openvurp_dir, "memory", ".restarted")
    if not os.path.exists(restarted_path):
        return ""
    try:
        with open(restarted_path, "r", encoding="utf-8") as f:
            reason = f.read().strip()
        os.remove(restarted_path)
        return reason or "restart"
    except OSError:
        return ""


def _status_icon(msg: str) -> str:
    """Sceglie un'icona in base al contenuto del messaggio di status."""
    low = msg.lower()
    for key, icon in _STATUS_ICONS.items():
        if key in low:
            return icon
    return "🔧"


class ResponseCollector:
    """Cattura la risposta dell'agente per chi non ha una console davanti.

    Serviva al bot Telegram (typing, status, conferme a bottoni). Con il canale
    in entrata rimosso resta solo il mestiere che aveva davvero: raccogliere il
    testo per l'heartbeat, e far vedere i passaggi sul terminale.
    """

    def __init__(self, real_ui, chat_id: str = ""):
        self.real_ui = real_ui
        self.response_text = ""
        self._capturing = False
        self._tool_count = 0

    def start_spinner(self, msg=""):
        if msg:
            self.status(msg)

    def stop_spinner(self):
        pass

    def start_response(self):
        self._capturing = True
        self.response_text = ""

    def end_response(self):
        self._capturing = False

    def stream_text(self, text):
        if self._capturing:
            self.response_text += text

    def _note(self, renderable):
        """Output sul CLI prompt-safe (non rompe il box di input)."""
        notify = getattr(self.real_ui, "notify", None)
        if callable(notify):
            notify(renderable)
        else:
            self.real_ui.console.print(renderable)

    def status(self, msg):
        self._note(f"    [dim]{msg}[/dim]")

    def show_cmd(self, cmd):
        self._tool_count += 1
        self._note(f"    [dim]$ {cmd[:100]}[/dim]")

    def show_output(self, output, is_error=False):
        pass

    def error(self, msg):
        self.response_text += f"[Errore: {msg}]"
        self._note(f"    [red]{msg}[/red]")

    def openvurp_say(self, msg):
        self.response_text += msg

    def confirm(self, msg):
        """Senza nessuno a cui chiedere, il silenzio vale no."""
        self._note(f"  [yellow]Bloccato, nessuno a cui chiedere: {msg[:100]}[/yellow]")
        return False

    def show_evolve(self):
        pass


def start_heartbeat_background(agent, ui):
    """Avvia heartbeat in background."""
    try:
        from core.heartbeat import HeartbeatRunner, load_heartbeat_config
        from agent import OPENVURP_DIR
    except ImportError:
        return None

    config = load_heartbeat_config(OPENVURP_DIR)
    if not config.enabled:
        return None

    heartbeat = HeartbeatRunner(config, workspace_dir=OPENVURP_DIR)

    # Callback: esegui agente per heartbeat (loop completo con tool execution)
    def run_agent_for_heartbeat(prompt: str) -> str:
        with _agent_lock:
            try:
                collector = ResponseCollector(ui)
                old_ui = agent.ui
                agent.ui = collector
                try:
                    agent.run(prompt, source="heartbeat", sender="system", actor_id="cli_owner")
                finally:
                    agent.ui = old_ui
                return collector.response_text.strip() or "HEARTBEAT_OK"
            except Exception as e:
                return f"[Heartbeat error: {e}]"

    # Callback: invia messaggio
    def _print_heartbeat(text: str):
        # notify = prompt-safe: in coda se sei sul prompt, così non rompe il box
        from rich.text import Text as _RT
        block = _RT()
        block.append("\n  ♥ Heartbeat\n", style="bold magenta")
        block.append(f"  {text}\n")
        ui.notify(block)

    def _send_heartbeat_telegram(text: str) -> bool:
        """Notifica in uscita: e' cio' che ti raggiunge fuori casa."""
        try:
            from tools.notify import _get_telegram, _send_telegram
            token, chat_id = _get_telegram()
            if token and chat_id and _send_telegram(token, chat_id, text):
                return True
        except Exception:
            pass
        return False

    def send_heartbeat_message(target: str, text: str):
        if target == "log":
            _print_heartbeat(text)
            return
        if target == "telegram":
            if not _send_heartbeat_telegram(text):
                _print_heartbeat(text)
            return
        # target == "auto" (default): scrivi dove l'owner è davvero.
        # Attivo da poco sulla pagina → pagina; assente → notifica sul telefono.
        channel = ""
        try:
            if agent.presence is not None:
                available = ["cli", "telegram"]
                channel = agent.presence.pick_delivery_channel(available)
        except Exception:
            channel = ""
        if channel == "telegram" and _send_heartbeat_telegram(text):
            return
        _print_heartbeat(text)

    # Callback: eventi UI
    def on_heartbeat_event(event):
        if event.status.value == "alert":
            ui.console.print(f"  [dim]♥ heartbeat: alert inviato[/dim]")
        elif event.status.value == "failed":
            ui.console.print(f"  [dim]♥ heartbeat: error — {event.reason[:80]}[/dim]")
        # OK e SKIPPED sono silenziosi

    heartbeat.set_agent_callback(run_agent_for_heartbeat)
    heartbeat.set_send_callback(send_heartbeat_message)
    heartbeat.set_event_callback(on_heartbeat_event)
    # Il ciclo notturno indicizza anche nella memoria semantica
    heartbeat.memory_manager = agent.memory
    # Il ciclo notturno (sbiadire i ricordi, specchio) usa l'LLM dell'agente
    heartbeat.agent_ref = agent

    heartbeat.start()
    interval_min = config.interval_seconds // 60
    if QUIET_STARTUP:
        return heartbeat
    ui.console.print(f"  [green]Heartbeat on[/green] [dim](every {interval_min}min, "
                     f"{config.active_hours_start}:00-{config.active_hours_end}:00)[/dim]")
    return heartbeat


def start_sentinel_background(agent, ui, heartbeat):
    """Avvia la sentinella: si accorge quando internet/Ollama/Telegram cadono
    E quando tornano — avvisa l'owner, riattacca Telegram da sola e sveglia
    il heartbeat al ritorno così l'agente riprende il lavoro sospeso."""
    try:
        import config as _cfg
        from core.sentinel import Sentinel, check_internet, make_ollama_check
        from agent import OPENVURP_DIR as _workspace
    except Exception as e:
        ui.error(f"Sentinella non avviata: {e}")
        return None

    sentinel = Sentinel(_workspace)
    sentinel.add_probe("internet", check_internet, label="Internet")

    if str(getattr(_cfg, "LLM_BACKEND", "")) == "ollama":
        base_url = str(getattr(_cfg, "LLM_BASE_URL", "http://localhost:11434"))
        sentinel.add_probe("ollama", make_ollama_check(base_url), label="Ollama")

    def _notify_owner(text: str) -> bool:
        # Il canale in entrata non c'e' piu': resta la notifica in uscita.
        try:
            from tools.notify import _get_telegram, _send_telegram
            token, chat_id = _get_telegram()
            if token and chat_id:
                _send_telegram(token, chat_id, text)
        except Exception:
            pass
        try:
            ui.console.print(f"  [dim]🛰 sentinella: {text}[/dim]")
        except Exception:
            pass
        # La console È comunque il canale dell'owner: consegna riuscita.
        return True

    sentinel.set_notifier(_notify_owner)
    if heartbeat is not None:
        sentinel.attach_heartbeat(heartbeat)
    sentinel.start()
    # L'agente può pungolarla (check_now) quando vede il backend LLM giù.
    agent.sentinel = sentinel
    if not QUIET_STARTUP:
        ui.console.print("  [green]Sentinel on[/green] [dim](internet/ollama, auto-recovery)[/dim]")
    return sentinel


_shared_chat_fn = None


def shared_chat_fn(agent, ui):
    """La conversazione, una sola per tutto openvurp.

    La pagina web e i canali in entrata devono passare di qui, non ognuno per
    la sua strada: e' esattamente l'errore del vecchio bot Telegram, che aveva
    una propria idea di conversazione e per questo non ha mai saputo niente di
    rubrica, stanze e approvazioni.
    """
    global _shared_chat_fn
    if _shared_chat_fn is None:
        from dashboard import make_chat_fn
        # Lo stesso _agent_lock dei turni CLI → niente accessi concorrenti.
        _shared_chat_fn = make_chat_fn(agent, _agent_lock, ui)
    return _shared_chat_fn


def start_channels_background(agent, ui):
    """Avvia i canali in entrata elencati in CHANNELS_IN."""
    from core.channels_runtime import SUPERVISOR
    from core.conversation import ChannelConversation

    chat_fn = shared_chat_fn(agent, ui)
    store = getattr(chat_fn, "chat_store", None)
    if store is None:
        return []
    SUPERVISOR.bind(
        ChannelConversation(chat_fn, store, swarm=getattr(agent, "swarm", None)), ui)
    outcome = SUPERVISOR.apply()
    for problem in outcome.get("errors", []):
        ui.error(f"Channel {problem}")
    if outcome["running"] and not QUIET_STARTUP:
        ui.console.print(f"  [green]Inbound channels[/green] "
                         f"[dim]{' · '.join(outcome['running'])}[/dim]")
    return outcome["running"]


def start_dashboard_background(agent, ui, port=8420):
    """Avvia web dashboard in background (con chat collegata all'agente)."""
    try:
        import config as _cfg
        from dashboard import DashboardServer
        chat_fn = shared_chat_fn(agent, ui)
        host = str(getattr(_cfg, "DASHBOARD_HOST", "127.0.0.1") or "127.0.0.1")
        token = str(getattr(_cfg, "DASHBOARD_TOKEN", "") or "")
        server = DashboardServer(agent, port=port, chat_fn=chat_fn, host=host, token=token)
        try:
            server.bind()          # se la porta e' occupata lo sappiamo QUI
        except OSError as exc:
            ui.error(
                f"Porta {port} gia' occupata ({exc}). Con ogni probabilita' c'e' un "
                f"altro openvurp acceso: quello continuerebbe a servire la sua "
                f"versione della pagina. Chiudi l'istanza vecchia, oppure cambia "
                f"DASHBOARD_PORT nel .env."
            )
            return None
        threading.Thread(target=server.start, daemon=True, name="dashboard").start()
        return server
    except Exception as e:
        ui.error(f"Dashboard error: {e}")
        return None


def start_gateway_background(ui, host="127.0.0.1", port=8421):
    """Avvia runtime gateway HTTP locale separato dal loop agente."""
    try:
        from core.runtime_api import RuntimeAPIServer
        from agent import OPENVURP_DIR
        server = RuntimeAPIServer(OPENVURP_DIR, host=host, port=port)
        t = threading.Thread(target=server.start, daemon=True, name="runtime-gateway")
        t.start()
        ui.console.print(f"  [green]Runtime gateway[/green] [dim]http://{host}:{port}[/dim]")
        return server
    except Exception as e:
        ui.error(f"Gateway error: {e}")
        return None


def _run_cmd(cmd: str, timeout: int = 10) -> str:
    """Esegue un comando e ritorna l'output (o stringa vuota se fallisce)."""
    import subprocess
    try:
        r = subprocess.run(
            cmd, shell=True, capture_output=True, text=True,
            timeout=timeout, encoding="utf-8", errors="replace",
        )
        return (r.stdout or "").strip()
    except Exception:
        return ""


def _has_internet(timeout: float = 3.0) -> bool:
    """Connettività reale: prova un TCP/443 verso più host pubblici affidabili.
    True se ALMENO uno risponde. Robusto: niente dipendenza da `curl` o da un
    singolo host (il vecchio check su httpbin.org dava falsi 'NO internet')."""
    import socket
    targets = [
        ("1.1.1.1", 443),       # Cloudflare (IP: no DNS necessario)
        ("8.8.8.8", 443),       # Google DNS over TLS (IP)
        ("github.com", 443),    # fallback con risoluzione DNS
        ("www.google.com", 443),
    ]
    for host, port in targets:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            continue
    return False


def _run_system_discovery(ui) -> dict:
    """Esplora il sistema e ritorna un dict strutturato con i risultati reali."""
    import shutil
    import platform

    ui.console.print(f"  [dim]  Controllo sistema...[/dim]")

    report = {
        "os": {},
        "hardware": {},
        "user": {},
        "network": {},
        "software": {"languages": {}, "editors": {}, "tools": {}, "databases": {}, "package_managers": {}},
        "ollama": {"installed": False, "models": [], "vision_models": []},
        "whisper": {"installed": False, "version": ""},
    }

    # OS
    report["os"]["platform"] = platform.system()
    report["os"]["version"] = platform.version()
    report["os"]["release"] = platform.release()
    is_wsl = bool(os.environ.get("WSL_DISTRO_NAME"))
    report["os"]["is_wsl"] = is_wsl
    ver = _run_cmd("ver") if platform.system() == "Windows" else _run_cmd("uname -a")
    report["os"]["detail"] = ver

    # Hardware
    ui.console.print(f"  [dim]  Controllo hardware...[/dim]")
    if platform.system() == "Windows":
        report["hardware"]["cpu"] = _run_cmd('wmic cpu get name /format:list').replace("Name=", "")
        report["hardware"]["cores"] = _run_cmd('echo %NUMBER_OF_PROCESSORS%')
        ram_kb = _run_cmd('wmic OS get TotalVisibleMemorySize /value').replace("TotalVisibleMemorySize=", "")
        try:
            report["hardware"]["ram_gb"] = round(int(ram_kb) / 1024 / 1024, 1)
        except (ValueError, TypeError):
            report["hardware"]["ram_gb"] = "?"
        disk_info = _run_cmd('wmic logicaldisk where "DeviceID=\'C:\'" get Size,FreeSpace /value')
        for line in disk_info.splitlines():
            if line.startswith("FreeSpace="):
                try:
                    report["hardware"]["disk_free_gb"] = round(int(line.split("=")[1]) / 1024**3, 1)
                except (ValueError, TypeError):
                    pass
            if line.startswith("Size="):
                try:
                    report["hardware"]["disk_total_gb"] = round(int(line.split("=")[1]) / 1024**3, 1)
                except (ValueError, TypeError):
                    pass
    else:
        report["hardware"]["cpu"] = _run_cmd("cat /proc/cpuinfo | grep 'model name' | head -1").split(":")[-1].strip()
        report["hardware"]["cores"] = _run_cmd("nproc")
        mem = _run_cmd("free -m | grep Mem")
        if mem:
            parts = mem.split()
            try:
                report["hardware"]["ram_gb"] = round(int(parts[1]) / 1024, 1)
            except (ValueError, IndexError):
                pass
        disk = _run_cmd("df -h / | tail -1")
        report["hardware"]["disk_info"] = disk

    report["hardware"]["gpu"] = _run_cmd("nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null") or "nessuna GPU dedicata"

    # User
    report["user"]["name"] = _run_cmd("whoami")
    report["user"]["hostname"] = _run_cmd("hostname")
    if platform.system() == "Windows":
        report["user"]["timezone"] = _run_cmd("tzutil /g")
    else:
        report["user"]["timezone"] = _run_cmd("date +%Z")

    # Network
    ui.console.print(f"  [dim]  Controllo rete...[/dim]")
    report["network"]["internet"] = _has_internet()

    # Software
    ui.console.print(f"  [dim]  Controllo software...[/dim]")
    lang_checks = {
        "python": "python3 --version 2>/dev/null || python --version 2>/dev/null",
        "node": "node --version 2>/dev/null",
        "go": "go version 2>/dev/null",
        "rust": "rustc --version 2>/dev/null",
        "java": "java --version 2>/dev/null",
        "ruby": "ruby --version 2>/dev/null",
    }
    for name, cmd in lang_checks.items():
        v = _run_cmd(cmd)
        if v:
            report["software"]["languages"][name] = v.splitlines()[0]

    editor_checks = {
        "vscode": "code --version 2>/dev/null",
        "cursor": "cursor --version 2>/dev/null",
        "vim": "vim --version 2>/dev/null",
    }
    for name, cmd in editor_checks.items():
        v = _run_cmd(cmd)
        if v:
            report["software"]["editors"][name] = v.splitlines()[0]

    tool_checks = {
        "git": "git --version",
        "docker": "docker --version 2>/dev/null",
        "make": "make --version 2>/dev/null",
    }
    for name, cmd in tool_checks.items():
        v = _run_cmd(cmd)
        if v:
            report["software"]["tools"][name] = v.splitlines()[0]

    db_checks = {
        "sqlite": "sqlite3 --version 2>/dev/null",
        "postgres": "psql --version 2>/dev/null",
        "mysql": "mysql --version 2>/dev/null",
        "redis": "redis-cli --version 2>/dev/null",
    }
    for name, cmd in db_checks.items():
        v = _run_cmd(cmd)
        if v:
            report["software"]["databases"][name] = v.splitlines()[0]

    pm_checks = {"pip": "pip --version", "uv": "uv --version", "npm": "npm --version"}
    for name, cmd in pm_checks.items():
        v = _run_cmd(cmd + " 2>/dev/null")
        if v:
            report["software"]["package_managers"][name] = v.splitlines()[0]

    # Ollama
    ui.console.print(f"  [dim]  Controllo Ollama...[/dim]")
    ollama_ver = _run_cmd("ollama --version 2>/dev/null")
    if ollama_ver:
        report["ollama"]["installed"] = True
        report["ollama"]["version"] = ollama_ver
        tags_raw = _run_cmd("curl -s http://localhost:11434/api/tags 2>/dev/null")
        if tags_raw:
            try:
                tags = json.loads(tags_raw)
                models = [m["name"] for m in tags.get("models", [])]
                report["ollama"]["models"] = models
                vision_kw = ["llava", "moondream", "qwen-vl", "qwen2-vl", "qwen3-vl", "bakllava", "minicpm-v", "glm-ocr"]
                report["ollama"]["vision_models"] = [m for m in models if any(v in m.lower() for v in vision_kw)]
            except (json.JSONDecodeError, KeyError):
                pass

    # Whisper — import diretto
    ui.console.print(f"  [dim]  Controllo Whisper...[/dim]")
    try:
        import whisper as _wh
        whisper_ver = getattr(_wh, "__version__", "ok")
        report["whisper"]["installed"] = True
        report["whisper"]["version"] = whisper_ver
    except ImportError:
        pass

    # Voice (TTS + microfono) — import diretto, non subprocess
    ui.console.print(f"  [dim]  Controllo voce...[/dim]")
    report["voice"] = {"tts_installed": False, "mic_installed": False}
    try:
        import edge_tts as _et
        edge_tts_ver = getattr(_et, "__version__", "ok")
        report["voice"]["tts_installed"] = True
        report["voice"]["tts_version"] = edge_tts_ver
    except ImportError:
        pass
    try:
        import sounddevice as _sd
        sd_ver = getattr(_sd, "__version__", "ok")
        report["voice"]["mic_installed"] = True
        report["voice"]["mic_version"] = sd_ver
    except ImportError:
        pass

    ui.console.print(f"  [dim]  Exploration complete.[/dim]")
    return report


def run_startup_menu(args) -> bool:
    """Schermata di scelta all'avvio. Ritorna False se si deve uscire.

    Finora `openvurp` partiva dritto in chat e l'unico modo di cambiare motore
    era editare il .env: le scelte c'erano ma non erano raggiungibili.
    """
    import config
    from core.setup_wizard import (
        SUBSCRIPTION_BACKENDS, current_config, quick_engine_menu,
        run_wizard, subscription_login_status,
    )

    try:
        from rich.console import Console
        from rich.prompt import Prompt
        console = Console()
    except Exception:
        console = None

    while True:
        cur = current_config()
        backend = cur.get("LLM_BACKEND", config.LLM_BACKEND)
        model = cur.get("LLM_MODEL", config.LLM_MODEL)
        login = ""
        if backend in SUBSCRIPTION_BACKENDS:
            ok, detail = subscription_login_status(backend)
            login = ("✓ " if ok else "✗ ") + detail

        options = [
            ("1", "Open openvurp", "the wallet of agents, in your browser"),
            ("2", "Change engine", "backend and model, without redoing setup"),
            ("3", "Full setup", "engine, Telegram, voice"),
            ("4", "Diagnose", "doctor: what works and what doesn't"),
            ("5", "Quit", ""),
        ]
        if console is not None:
            console.print()
            console.print(f"  [bold yellow]✳ openvurp[/bold yellow] "
                          f"[dim]wallet for agents · {backend} · {model}[/dim]"
                          + (f"  [dim]{login}[/dim]" if login else ""))
            console.print()
            for key, label, hint in options:
                console.print(f"   [cyan]{key}[/cyan]  {label:<18} [dim]{hint}[/dim]")
            console.print()
            choice = Prompt.ask("  [cyan]choose[/cyan]",
                                choices=[o[0] for o in options], default="1")
        else:
            print(f"\n✳ openvurp — wallet for agents — {backend} / {model} {login}")
            for key, label, hint in options:
                print(f"  {key}  {label:<18} {hint}")
            choice = input("choose [1]: ").strip() or "1"

        if choice == "1":
            return True
        if choice == "2":
            quick_engine_menu(console)
            _reload_engine_config()
            continue
        if choice == "3":
            run_wizard(force=True)
            _reload_engine_config()
            continue
        if choice == "4":
            from core.doctor import build_doctor_report
            from agent import OPENVURP_DIR as _dir
            print(build_doctor_report(_dir, []).render())
            continue
        return False


def _reload_engine_config() -> None:
    """Rilegge dal .env i valori che il menu può aver appena cambiato."""
    import config
    from core.setup_wizard import current_config

    cur = current_config()
    for key in ("LLM_BACKEND", "LLM_MODEL", "LLM_BASE_URL"):
        if cur.get(key):
            setattr(config, key, cur[key])
            os.environ[key] = cur[key]


CLI_HELP = """
  [bold]Comandi[/bold] [dim](scrivi qualsiasi altra cosa per parlare con l'agente)[/dim]

   [cyan]/[/cyan] [cyan]/help[/cyan]        questa lista
   [cyan]/setup[/cyan]           bootstrap runtime · [cyan]openvurp --setup[/cyan] per il wizard completo
   [cyan]/doctor[/cyan]          diagnosi: cosa funziona e cosa no
   [cyan]/mode[/cyan] <m>        safe · auto · plan
   [cyan]/self[/cyan] [cyan]/trace[/cyan]   chi sono ora · cosa ho fatto in questo turno
   [cyan]/memory[/cyan] [cyan]/skills[/cyan] memoria e skill attive

  [bold]Sciame[/bold] [dim](specialisti creati dall'agente o da te)[/dim]
   [cyan]/swarm[/cyan]                    elenco e aiuto
   [cyan]/swarm new[/cyan] <nome> <ruolo> convoca uno specialista
   [cyan]@nome[/cyan] <messaggio>         scrivi a UNO
   [cyan]/swarm all[/cyan] <messaggio>    scrivi a TUTTI
   [cyan]/swarm discuss[/cyan] <tema>     falli discutere fra loro

  [bold]Vita interiore[/bold]

  [bold]Sistema[/bold]
   [cyan]/voice[/cyan] [cyan]/audio[/cyan] [cyan]/mic[/cyan] [cyan]/dashboard[/cyan] [cyan]/integrity[/cyan] [cyan]/update[/cyan] [cyan]/restart[/cyan] [cyan]/exit[/cyan]
"""


SWARM_HELP = """Sciame — specialisti persistenti con cui tu e l'agente potete parlare.

  /swarm                          elenca gli specialisti
  /swarm new <nome> <ruolo...>    convoca un nuovo specialista
  /swarm ask <nome> <messaggio>   scrivi a UNO   (scorciatoia: @nome messaggio)
  /swarm all <messaggio>          scrivi a TUTTI (rispondono indipendentemente)
  /swarm discuss [n] <argomento>  falli discutere fra loro per n giri (default 2)
  /swarm bye <nome>               congeda uno specialista
  /swarm log [n]                  ultimi scambi
"""


def _addresses_swarm_member(text: str, agent) -> bool:
    """True solo se `@nome` corrisponde davvero a uno specialista esistente."""
    swarm = getattr(agent, "swarm", None)
    if swarm is None or not text.startswith("@") or " " not in text:
        return False
    from core.swarm import SwarmError
    try:
        swarm.resolve(text[1:].split(" ", 1)[0])
    except SwarmError:
        return False
    return True


def _handle_swarm_command(raw: str, agent, ui) -> None:
    """Comandi `/swarm` della CLI.

    L'agente puo' gia' convocare specialisti da solo con i tool; questi comandi
    servono a te: parlare direttamente a uno, a un altro, o a tutti insieme
    senza passare dall'agente principale. Il rendering e' lo stesso che vedono
    Telegram e gli altri canali: una sola verita' per tutti.
    """
    if getattr(agent, "swarm", None) is None:
        ui.console.print(
            "  [yellow]Sciame non attivo.[/yellow] [dim]Imposta SWARM_ENABLED=true nel .env.[/dim]"
        )
        return
    talking = any(raw.lower().startswith(f"/swarm {sub}") for sub in ("ask", "all", "discuss"))
    if talking or raw.startswith("@"):
        ui.start_spinner("Lo sciame sta pensando...")
        try:
            output = render_swarm_command(raw, agent)
        finally:
            ui.stop_spinner()
    else:
        output = render_swarm_command(raw, agent)
    ui.console.print(output)


def app_main():
    """Entry-point `openvurp`: apre openvurp con una schermata pulita.

    Pulisce lo schermo all'avvio (parte ordinato, come un'app) MA senza usare
    l'alternate screen: così la scrollback del terminale resta usabile — puoi
    salire nel testo con la rotella del mouse / Shift+PgUp. Solo in sessione
    interattiva; per --doctor/--headless/prompt diretto non tocca nulla.
    """
    args = parse_args()
    one_shot = bool(args.prompt) or args.doctor or args.doctor_fix or args.headless
    if (os.isatty(0) and os.isatty(1)) and not one_shot:
        sys.stdout.write("\033[H\033[2J")  # cursore in alto + pulisci la schermata
        sys.stdout.flush()
    return main()


def main():
    args = parse_args()

    # Onboarding guidato: alla prima apertura (o con --setup) niente .env a mano.
    # Gira prima di tutto, in terminale normale. Saltato in headless (Docker usa
    # le variabili d'ambiente del container).
    if not args.headless and (os.isatty(0) and os.isatty(1)):
        from core.setup_wizard import needs_setup, run_wizard
        try:
            # Parte su `openvurp` (senza --setup) SOLO se manca la config (prima
            # volta). Se hai già configurato, non compare nulla. `--setup` lo
            # forza a mano quando vuoi riconfigurare.
            run_wizard(force=getattr(args, "setup", False))
        except (KeyboardInterrupt, EOFError):
            print()
        except Exception as exc:
            print(f"[setup interrotto: {exc}]")
        # Da zero il setup è un REQUISITO: se manca ancora la configurazione
        # minima (wizard abbandonato o fallito) non svegliamo un agente a metà.
        if needs_setup():
            print("Configurazione incompleta — l'agente non parte senza. "
                  "Rilancia `openvurp` per rifare il setup.")
            return

        # Menu di avvio: le scelte devono essere davanti agli occhi, non
        # sepolte in un file .env da modificare a mano.
        import config as _cfg_menu
        show_menu = getattr(args, "menu", False) or (
            getattr(_cfg_menu, "STARTUP_MENU", True)
            and not args.prompt
            and not getattr(args, "no_menu", False)
            and not getattr(args, "setup", False)
            and not getattr(args, "headless", False)
        )
        if show_menu:
            try:
                if not run_startup_menu(args):
                    return
            except (KeyboardInterrupt, EOFError):
                print()
                return

    # Abilita readline: editing della riga, storia e — importante — incollaggio
    # multilinea (bracketed paste) senza che ogni newline venga inviato a parte.
    try:
        import readline
        readline.parse_and_bind("set enable-bracketed-paste on")
    except Exception:
        pass

    import config
    if args.model:
        config.LLM_MODEL = args.model
    if args.backend:
        config.LLM_BACKEND = args.backend

    from agent import UI, OPENVURP_DIR, MEMORY_DIR, load_file
    from core.agent import Agent

    setup_report = ensure_runtime_state(
        OPENVURP_DIR,
        allowed_telegram_users=list(getattr(config, "TELEGRAM_ALLOWED_USERS", []) or []),
        create_integrity_baseline=False,
        force_acl_refresh=False,
    )

    ui = UI()

    # In modalita' web il terminale non e' l'interfaccia: e' il posto da cui
    # leggere l'indirizzo. Il banner grande, gli avvisi di capacita' e la
    # diagnostica appartengono a --doctor, non a un avvio riuscito.
    global QUIET_STARTUP
    web_mode = not args.headless and not args.prompt
    QUIET_STARTUP = web_mode
    if not web_mode:
        ui.welcome(model=config.LLM_MODEL, backend=config.LLM_BACKEND)
    if setup_report.changed and not web_mode:
        ui.console.print("  [dim]Runtime bootstrap iniziale applicato.[/dim]")

    agent = Agent(ui=ui)
    ui._approval_mode = agent.approval_mode
    if agent.approval_mode != "safe":
        ui.console.print(f"  [dim]mode: {agent.approval_mode}[/dim]")
    if hasattr(agent, "gateway"):
        agent.gateway.register_announcer("cli", lambda _route, text: ui.openvurp_say(text))
    capability_report = inspect_runtime_capabilities(agent.tools.names())
    if capability_report.warnings and not web_mode:
        for warning in capability_report.warnings:
            ui.console.print(f"  [yellow]Capability warning:[/yellow] {warning}")

    if args.doctor:
        from core.doctor import build_doctor_report
        ui.openvurp_say(build_doctor_report(OPENVURP_DIR, agent.tools.names()).render())
        return
    if args.doctor_fix:
        from core.doctor import fix_runtime_issues
        report = fix_runtime_issues(
            OPENVURP_DIR,
            allowed_telegram_users=list(getattr(config, "TELEGRAM_ALLOWED_USERS", []) or []),
        )
        ui.openvurp_say(report.render())
        return

    # ── Dashboard ──
    dashboard = None
    # openvurp E' l'interfaccia web, punto: la pagina parte sempre. C'era un
    # `--cli` che apriva una chat da terminale al suo posto, e una TUI intera
    # come terza porta. Una sola porta, quella che si usa davvero.
    dashboard_enabled = True
    if dashboard_enabled:
        dashboard_port = getattr(config, 'DASHBOARD_PORT', 8420)
        dashboard = start_dashboard_background(agent, ui, port=dashboard_port)
    channels = start_channels_background(agent, ui)

    gateway = None
    gateway_enabled = getattr(config, 'GATEWAY_ENABLED', False) or args.gateway or args.headless
    if gateway_enabled:
        gateway = start_gateway_background(
            ui,
            host=str(getattr(config, "GATEWAY_HOST", "127.0.0.1") or "127.0.0.1"),
            port=int(getattr(config, "GATEWAY_PORT", 8421) or 8421),
        )

    # ── Heartbeat ──
    heartbeat = start_heartbeat_background(agent, ui)

    # ── Sentinella (internet/ollama: caduta E ritorno) ──
    sentinel = start_sentinel_background(agent, ui, heartbeat)

    # ── Chiacchiere fra agenti ──
    # Non serve un motivo: ogni tanto due di loro si dicono qualcosa da soli.
    if getattr(agent, "swarm", None) is not None:
        try:
            agent.swarm.start_small_talk(None if QUIET_STARTUP else ui)
        except Exception:
            pass

    # ── Scheduler (messaggi programmati) ──
    from tools.scheduler import start_scheduler
    start_scheduler()

    # ── Restart watcher (background) ──
    # Sorveglia il sentinel memory/.restart scritto da: tool request_restart
    # dell'agente, /restart Telegram, auto-update. Indipendente dal loop di input,
    # così il riavvio avviene anche se nessuno sta scrivendo nella CLI.
    def _restart_watcher():
        from core import updater
        while True:
            time.sleep(2)
            if not updater.restart_pending():
                continue
            reason = updater.consume_restart()
            try:
                agent.save_session()
            except Exception:
                pass
            try:
                sys.stdout.write("\033[?2004l")  # disattiva bracketed paste
                sys.stdout.flush()
            except Exception:
                pass
            try:
                ui.console.print(f"\n  [bold cyan]↻ restarting… ({reason})[/bold cyan]")
            except Exception:
                pass
            if os.environ.get("OPENVURP_UNDER_WATCHER"):
                os._exit(42)  # il watcher rilancia il sottoprocesso
            try:
                updater.restart_in_place()
            except Exception:
                os._exit(42)
    threading.Thread(target=_restart_watcher, daemon=True).start()

    # ── Controlla se è un riavvio ──
    restart_reason = check_restarted(OPENVURP_DIR)
    is_restart = bool(restart_reason)

    if is_restart:
        restart_detail = restart_reason.splitlines()[-1][:100]
        ui.console.print(f"  [dim]Restart detected: {restart_detail}[/dim]")
        try:
            from tools.notify import notify_handler
            notify_handler(f"openvurp restarted. Reason: {restart_detail}")
        except Exception:
            pass
        # Inietta il contesto del riavvio nell'agente — così sa cosa è successo
        agent._restart_context = restart_detail
        # Ripristina la conversazione precedente — continua da dove eravamo
        agent.restore_conversation()

    # ── Saluto d'avvio ──
    profile_path = os.path.join(MEMORY_DIR, "profilo.json")
    name = ""
    try:
        if os.path.exists(profile_path):
            p = json.loads(load_file(profile_path))
            name = p.get("nome", p.get("name", ""))
    except Exception:
        pass
    # Il saluto ha senso dove qualcuno lo legge: in modalita' web la chat e'
    # nel browser, e un monologo nel terminale sarebbe solo token spesi.
    web_mode = not args.headless and not args.prompt
    with _agent_lock:
        if web_mode:
            pass
        elif is_restart:
            agent.run(
                f"[SISTEMA] Ti sei appena riavviato. Motivo: {restart_detail}. "
                f"L'utente si chiama {name or 'non lo sai ancora'}. "
                f"Saluta brevemente, di' che ti sei aggiornato. Sii naturale, breve."
            )
        else:
            agent.run(
                f"[SISTEMA] Nuova sessione. L'utente si chiama {name or 'non lo sai ancora'}. "
                f"Saluta in modo naturale e breve. Sii te stesso. "
                f"Non chiedere cosa fare — aspetta che l'utente parli."
            )

    # ── Prompt da CLI ──
    if args.prompt:
        with _agent_lock:
            agent.run(" ".join(args.prompt))
        agent.save_session()
        return

    # ── Interfaccia web (default) ──
    # `openvurp` apre il portafoglio degli agenti nel browser. Il terminale
    # e' l'unica porta: la chat da terminale e la TUI non ci sono piu'.
    if not args.headless:
        port = int(getattr(config, 'DASHBOARD_PORT', 8420) or 8420)
        url = f"http://localhost:{port}/"
        token = str(getattr(config, "DASHBOARD_TOKEN", "") or "")
        if token:
            url += f"?token={token}"
        if dashboard is None:
            ui.console.print(
                "  [red]Interfaccia web non avviata.[/red] "
                "[dim]Controlla la porta con `openvurp --doctor`.[/dim]"
            )
            return
        ui.console.print(f"\n  [bold]openvurp[/bold] [dim]— wallet for agents[/dim]")
        ui.console.print(f"  [cyan]{url}[/cyan]")
        services = [n for n, on in (
            ("heartbeat", heartbeat is not None),
            ("sentinella", sentinel is not None),
        ) if on]
        if services:
            ui.console.print(f"  [dim]running: {' · '.join(services)}[/dim]")
        ui.console.print("  [dim]Ctrl+C per fermare[/dim]\n")
        if not args.no_browser:
            try:
                import webbrowser
                webbrowser.open(url)
            except Exception:
                pass
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            ui.console.print("\n  [dim]Chiudo openvurp…[/dim]")
            agent.save_session()
            agent.cleanup()
            if heartbeat:
                heartbeat.stop()
            if sentinel:
                sentinel.stop()
            return

    if args.headless:
        ui.console.print("  [green]openvurp headless[/green] [dim]— interact via dashboard. Ctrl+C to stop.[/dim]")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            ui.console.print("\n  [dim]Stopping openvurp…[/dim]")
            agent.save_session()
            agent.cleanup()
            return

if __name__ == "__main__":
    main()
