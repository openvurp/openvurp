"""
openvurp 4.0 — Entry point.

Integra core/ per tool system, reasoning, safety, observability.
Telegram parte automaticamente se TELEGRAM_TOKEN e configurato.
Sessioni separate per CLI e Telegram.
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
    parser.add_argument("--no-telegram", action="store_true", help="Do not start Telegram")
    parser.add_argument("--dashboard", action="store_true", help="Start web dashboard")
    parser.add_argument("--gateway", action="store_true", help="Start local HTTP runtime gateway")
    parser.add_argument("--doctor", action="store_true", help="Print runtime diagnostics and exit")
    parser.add_argument("--doctor-fix", action="store_true", help="Apply full runtime bootstrap, then exit")
    parser.add_argument("--headless", action="store_true",
                        help="Start services (dashboard/gateway/telegram/heartbeat) without interactive loop — for Docker/server")
    parser.add_argument("--setup", action="store_true",
                        help="Run guided setup (backend/model/Telegram), then start")
    return parser.parse_args()


# Lock per accesso thread-safe all'agent
_agent_lock = threading.Lock()


def finalize_channel_response(text: str, source: str) -> str:
    """Normalizza una risposta di callback mantenendo i control token utili."""
    return format_callback_response(text, source=source)


def render_command(text, agent, openvurp_dir, memory_dir):
    """Rende l'output di un comando-pannello come testo (per Telegram & co).

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
        if t.startswith("/anima"):
            return agent.anima.render_status() if getattr(agent, "anima", None) else "Anima not available."
        if t.startswith("/growth"):
            from core.growth import build_growth_report
            return build_growth_report(memory_dir, days=max(1, min(_num(7), 365)),
                                       memory_manager=getattr(agent, "memory", None)).render()
        if t.startswith("/diary") or t.startswith("/diario"):
            from core.diary import render_diary
            return render_diary(memory_dir, limit=max(1, min(_num(7), 60)))
        if t.startswith("/patti") or t.startswith("/pacts"):
            return agent.pacts.render_status() if getattr(agent, "pacts", None) else "Pacts not available."
        if t.startswith("/specchio") or t.startswith("/mirror"):
            from core.mirror import Mirror
            m = Mirror(memory_dir)
            m.harvest()
            return m.render_status()
        if t.startswith("/fili") or t.startswith("/legame"):
            return agent.bonds.render_status() if getattr(agent, "bonds", None) else "Bond not available."
        if t.startswith("/sensi") or t.startswith("/senses"):
            return agent.senses.render_status() if getattr(agent, "senses", None) else "Senses not available."
        if t.startswith("/progetti") or t.startswith("/projects"):
            return agent.projects.render_status() if getattr(agent, "projects", None) else "Projects not available."
        if t.startswith("/fucina") or t.startswith("/forge"):
            return agent.forge.render_status() if getattr(agent, "forge", None) else "Forge not available."
        if t.startswith("/curiosita") or t.startswith("/curiosity"):
            return agent.curiosity.render_status() if getattr(agent, "curiosity", None) else "Curiosity not available."
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


def should_run_bootstrap(openvurp_dir: str, is_restart: bool = False) -> bool:
    """BOOTSTRAP.md è la fonte di verità per capire se l'agente è appena nato.
    Ma se è un riavvio del watcher, non è un primo avvio."""
    bootstrap_path = os.path.join(openvurp_dir, "BOOTSTRAP.md")
    if not os.path.exists(bootstrap_path):
        return False
    if is_restart:
        return False
    return True


def read_identity_name(openvurp_dir: str, load_file_fn) -> str:
    """Estrae il nome agente da IDENTITY.md se presente."""
    identity_path = os.path.join(openvurp_dir, "IDENTITY.md")
    if not os.path.exists(identity_path):
        return ""

    try:
        content = load_file_fn(identity_path)
    except Exception:
        return ""

    markers = ("- **Nome:**", "- **Name:**")
    for raw_line in content.splitlines():
        line = raw_line.strip()
        for marker in markers:
            if not line.lower().startswith(marker.lower()):
                continue
            value = line[len(marker):].strip()
            return value
    return ""


def start_telegram_background(agent, ui, token):
    """Avvia Telegram in background con rate limiting e sessioni separate."""
    try:
        from channels.telegram import TelegramChannel
    except ImportError:
        ui.error("python-telegram-bot not installed. Telegram disabled.")
        return None

    from core.rate_limit import RateLimiter
    rate_limiter = RateLimiter(cooldown_seconds=2.0, max_burst=5, burst_window=60)

    try:
        telegram = TelegramChannel(token=token, on_error=ui.error)
    except ValueError as e:
        ui.error(str(e))
        return None

    if hasattr(agent, "gateway"):
        def announce_to_telegram(route, text):
            telegram.send(
                text,
                chat_id=getattr(route, "chat_id", "") or None,
                thread_id=getattr(route, "thread_id", "") or "",
            )
        agent.gateway.register_announcer("telegram", announce_to_telegram)

    import config as _tg_config
    from agent import OPENVURP_DIR, MEMORY_DIR, load_file
    from core.group_chat import GroupChatBuffer
    allowed_users = getattr(_tg_config, 'TELEGRAM_ALLOWED_USERS', [])
    _group_buffer = GroupChatBuffer(  # memoria per gruppo, persistente su disco
        maxlen=50,
        persist_path=os.path.join(MEMORY_DIR, "group_memory.json"),
    )

    def handle_message(msg):
        """Processa messaggi Telegram con sessione separata."""
        text = msg.text
        sender = msg.sender or "Telegram"
        raw = msg.raw
        user_id = None
        if hasattr(raw, 'message') and raw.message and raw.message.from_user:
            user_id = raw.message.from_user.id
        elif isinstance(raw, dict):
            user_id = raw.get("message", {}).get("from", {}).get("id")
        actor_id = f"telegram:{user_id}" if user_id is not None else f"telegram:{sender}"

        # ── Gruppo: MEMORIZZA tutto e, se non sei taggato, silenzio SUBITO ──
        # Registra ogni messaggio (di chiunque, owner o no) PRIMA di ogni filtro,
        # così l'agente "legge tutto" il gruppo. Se non è interpellato esce qui:
        # il messaggio resta in memoria ma niente rate-limit, parsing comandi o
        # rumore sul CLI — e soprattutto nessuna risposta indesiderata al gruppo.
        if getattr(msg, "chat_type", "private") in ("group", "supergroup"):
            # Whitelist gruppi: se configurata, partecipa SOLO ai gruppi elencati.
            # Fuori whitelist = ignora del tutto (niente risposta, niente memoria).
            # Stampa il chat_id una volta per gruppo così l'owner può aggiungerlo.
            _wl = getattr(_tg_config, "TELEGRAM_GROUP_WHITELIST", []) or []
            _cid = str(msg.chat_id or "")
            if _wl and _cid not in {str(x) for x in _wl}:
                _seen = getattr(handle_message, "_wl_hinted", None)
                if _seen is None:
                    _seen = set()
                    handle_message._wl_hinted = _seen
                if _cid and _cid not in _seen:
                    _seen.add(_cid)
                    ui.console.print(
                        f"  [yellow][gruppo fuori whitelist] chat_id={_cid} — "
                        f"aggiungilo a TELEGRAM_GROUP_WHITELIST in .env per "
                        f"farlo partecipare.[/yellow]"
                    )
                return None  # gruppo non autorizzato: ignorato del tutto
            if msg.chat_id:
                _group_buffer.add(str(msg.chat_id), sender, text)
                # Roster: impara chi c'è nel gruppo (per sapere chi c'è e taggarlo)
                _group_buffer.note_person(str(msg.chat_id), sender, getattr(msg, "username", ""))
            _grp_mode = getattr(_tg_config, "TELEGRAM_GROUP_MODE", "mention")
            if not getattr(msg, "addressed", True) and _grp_mode != "all":
                # Non taggato (@bot o reply). Due vie per intervenire comunque:
                #  1) il NOME dell'agente compare nel testo ("Luna, che dici?"):
                #     riflesso umano, deterministico, vale in OGNI modalità.
                #  2) modalità 'natural': un modello-guardiano piccolo decide se
                #     è naturale intervenire, con cooldown per non essere molesto.
                from core.group_chat import (
                    name_mentioned, decide_intervention, get_decider_llm,
                )
                _name = read_identity_name(OPENVURP_DIR, load_file)
                _join = bool(_name) and name_mentioned(text, _name)
                if not _join and _grp_mode == "natural" and _cid:
                    _cool = getattr(_tg_config, "TELEGRAM_GROUP_COOLDOWN", 90)
                    if _group_buffer.cooldown_ok(_cid, _cool):
                        _decider = get_decider_llm(fallback=agent.llm)
                        _join = decide_intervention(
                            _decider, _name, _group_buffer.recent(_cid),
                            sender, text,
                        )
                if not _join:
                    # Feedback sul CLI: il messaggio resta in memoria anche se taccio.
                    ui.console.print(
                        f"  [dim][gruppo] memorizzato (silenzio): {sender}: {text[:60]}[/dim]"
                    )
                    return ""  # ambient: memorizzato, nessuna risposta
                msg.addressed = True  # decide di intervenire → flusso di risposta

        # Filtro user_id
        #   PRIVATO: solo gli owner ricevono risposta (anti-spam da estranei).
        #   GRUPPO: chiunque può ricevere risposta, ma come GUEST — RBAC lo tiene
        #   a "solo chat" (niente shell/file/web). Così natural/mention possono
        #   partecipare alla conversazione del gruppo, non solo all'owner.
        _in_group = getattr(msg, "chat_type", "private") in ("group", "supergroup")
        if allowed_users:
            if user_id not in allowed_users and not _in_group:
                return None  # privato da non-owner: ignora (resta in memoria)
        elif user_id is not None and not getattr(handle_message, "_hinted", False):
            # Lista vuota = nessun owner riconosciuto: su Telegram sei "guest"
            # (niente web/tool). Mostra l'ID da mettere in .env, una volta sola.
            handle_message._hinted = True
            ui.console.print(
                f"  [yellow]Telegram: nessun owner configurato — {sender} (id {user_id}) "
                f"opera come guest.[/yellow]\n"
                f"  [dim]Per pieni permessi aggiungi in .env: "
                f"TELEGRAM_ALLOWED_USERS={user_id}[/dim]"
            )

        allowed_channel, reason = agent.rbac.check_channel(actor_id, "telegram")
        if not allowed_channel:
            return reason

        # Comandi speciali
        if text.lower().startswith('/help'):
            return (
                "Comandi disponibili:\n"
                "/start — avvia o saluta\n"
                "/help — questa lista\n"
                "/status — modello, backend, uptime, token\n"
                "/doctor — diagnosi runtime/workspace\n"
                "/setup — bootstrap runtime serio\n"
                "/memory — mostra memoria\n"
                "/skills — mostra skill attive\n"
                "/restart — riavvia openvurp\n\n"
                "Oppure scrivi qualsiasi cosa e ti rispondo!"
            )

        if text.lower().startswith('/status'):
            import time as _time
            _uptime = int(_time.time() - agent._start_time) if hasattr(agent, '_start_time') else 0
            _h, _rem = divmod(_uptime, 3600)
            _m, _s = divmod(_rem, 60)
            _tokens = getattr(agent, 'total_tokens', 0)
            return (
                f"Modello: {_tg_config.LLM_MODEL}\n"
                f"Backend: {_tg_config.LLM_BACKEND}\n"
                f"Uptime: {_h}h {_m}m {_s}s\n"
                f"Token usati: {_tokens}"
            )

        if text.lower().startswith('/setup'):
            from core.doctor import fix_runtime_issues
            report = fix_runtime_issues(OPENVURP_DIR, allowed_telegram_users=allowed_users)
            agent.rbac = agent.rbac.__class__(MEMORY_DIR)
            return finalize_channel_response(report.render(), source="telegram")

        # Comandi-pannello (anima/growth/diary/patti/specchio/fili/sensi/progetti/
        # fucina/curiosita/integrity/doctor/memory/skills): stesso output della CLI.
        if text.strip().startswith('/'):
            panel = render_command(text, agent, OPENVURP_DIR, MEMORY_DIR)
            if panel is not None:
                return finalize_channel_response(panel, source="telegram")

        if text.lower().startswith('/restart'):
            from core import updater
            updater.request_restart("Restart from Telegram (/restart)")
            return "Restarting openvurp now…"

        if text.lower().startswith('/start'):
            profile_path = os.path.join(MEMORY_DIR, "profilo.json")
            bootstrap_path = os.path.join(OPENVURP_DIR, "BOOTSTRAP.md")
            if should_run_bootstrap(OPENVURP_DIR):
                tg_chat_id = msg.chat_id or ""
                bootstrap_content = load_file(bootstrap_path)
                with _agent_lock:
                    collector = ResponseCollector(ui, telegram_channel=telegram, chat_id=tg_chat_id)
                    old_ui = agent.ui
                    agent.ui = collector
                    try:
                        agent.run(
                            f"E il tuo primo avvio. BOOTSTRAP.md esiste — seguilo.\n\n"
                            f"Contenuto di BOOTSTRAP.md:\n\n{bootstrap_content}\n\n"
                            f"Inizia la conversazione con l'utente. Sii te stesso. Parla nella lingua dell'owner.\n"
                            f"Dopo il bootstrap, cancella BOOTSTRAP.md — non ti servira piu.",
                            source="telegram", sender=sender, actor_id=actor_id,
                            chat_id=tg_chat_id, thread_id=msg.thread_id,
                        )
                    finally:
                        agent.ui = old_ui
                    return finalize_channel_response(collector.response_text, source="telegram")
            else:
                name = ""
                agent_name = read_identity_name(OPENVURP_DIR, load_file)
                try:
                    if os.path.exists(profile_path):
                        p = json.loads(load_file(profile_path))
                        name = p.get("nome", p.get("name", ""))
                except Exception:
                    pass
                if name:
                    return f"Hi {name}!"
                if agent_name:
                    return f"Hi {sender}! I'm {agent_name}."
                return f"Hi {sender}!"

        # Rate limiting
        allowed, reason = rate_limiter.check(sender)
        if not allowed:
            return f"[{reason}]"

        # Mostra nel CLI
        ui.show_telegram_incoming(sender, text)

        # Usa sessione separata per Telegram
        # Estrai chat_id dal messaggio
        tg_chat_id = msg.chat_id or ""

        # ── Gruppi: leggi tutto, rispondi solo se taggato ──
        # L'agente MEMORIZZA ogni messaggio del gruppo (anche quelli non rivolti
        # a lui) nel buffer della chat. Così, quando viene interpellato, può
        # "rileggere" la conversazione recente per capire il contesto — inclusi
        # i tag verso altri utenti (es. "@alice"), che restano nel testo.
        # RISPONDE solo quando è interpellato: @menzione al bot o reply a un suo
        # messaggio (vedi should_respond_in_group in channels/telegram.py).
        # 'all' = rispondi a tutto (debug); qualsiasi altro valore = solo-tag.
        chat_type = getattr(msg, "chat_type", "private")
        addressed = getattr(msg, "addressed", True)
        if chat_type in ("group", "supergroup"):
            # Qui arrivano solo i messaggi TAGGATI (gli ambient sono già usciti in
            # cima, dopo essere stati memorizzati). Anteponi la chat recente così
            # l'agente rilegge il contesto prima di rispondere — gli altri
            # parlanti sono etichettati col loro nome e i tag @utente preservati.
            from core.group_chat import build_context_prefix
            recent = _group_buffer.recent(tg_chat_id)
            roster = _group_buffer.roster_text(tg_chat_id)  # chi c'è + come taggarlo
            ctx = build_context_prefix(recent)
            text = roster + ctx + text
            _group_buffer.mark_intervention(tg_chat_id)

        with _agent_lock:
            collector = ResponseCollector(ui, telegram_channel=telegram, chat_id=tg_chat_id)
            old_ui = agent.ui
            agent.ui = collector

            try:
                # source="telegram" → sessione separata
                agent.run(text, source="telegram", sender=sender, actor_id=actor_id, chat_id=tg_chat_id, thread_id=msg.thread_id, addressed=addressed, chat_type=chat_type)
                agent.session.save()
            except Exception as e:
                import traceback
                traceback.print_exc()
                collector.response_text = f"[Error: {str(e)[:200]}]"
            finally:
                agent.ui = old_ui
                # Pulisci typing e status su Telegram
                if telegram and tg_chat_id:
                    try:
                        telegram.stop_typing_loop(tg_chat_id)
                        telegram.clear_status(tg_chat_id)
                    except Exception:
                        pass

            response = finalize_channel_response(collector.response_text, source="telegram")
            directive = parse_response_directive(response)

        if directive.kind == "text":
            ui.show_telegram_outgoing(response)
            # Invia anche audio su Telegram solo se esplicitamente abilitato.
            if (
                getattr(_tg_config, 'TELEGRAM_VOICE_REPLY_ENABLED', False)
                and getattr(_tg_config, 'VOICE_ENABLED', False)
                and tg_chat_id
                and response.strip()
            ):
                try:
                    from voice import speak
                    import re
                    clean = re.sub(r'[*_`#\[\]()]', '', response)
                    clean = re.sub(r'\n{2,}', '. ', clean)
                    if len(clean) > 1000:
                        clean = clean[:1000] + "..."
                    audio_path = speak(clean, play=False)
                    if audio_path and os.path.exists(audio_path):
                        telegram.send_voice(tg_chat_id, audio_path)
                except ImportError:
                    pass
                except Exception as e:
                    ui.notify(f"  [dim][TG] Voice error: {e}[/dim]")
        elif directive.kind == "reaction":
            ui.notify(f"  [dim][TG] reaction {directive.emoji}[/dim]")

        return response

    telegram.on_message(handle_message)

    def run_telegram():
        try:
            telegram.start()
        except Exception as e:
            import traceback
            traceback.print_exc()
            ui.error(f"Telegram errore: {e}")

    t = threading.Thread(target=run_telegram, daemon=True, name="telegram")
    t.start()
    ui.console.print(f"  [green]Telegram on[/green] [dim](background)[/dim]")
    return telegram


_STATUS_ICONS = {
    "think": "🧠", "search": "🔍", "code": "💻", "write": "✍️",
    "read": "📖", "run": "⚙️", "web": "🌐", "memory": "🗂️",
    "done": "✅", "error": "❌", "wait": "⏳",
}


def _status_icon(msg: str) -> str:
    """Sceglie un'icona in base al contenuto del messaggio di status."""
    low = msg.lower()
    for key, icon in _STATUS_ICONS.items():
        if key in low:
            return icon
    return "🔧"


class ResponseCollector:
    """UI che cattura la risposta dell'agent + mostra tool nel CLI.

    Supporta conferma azioni via bottoni inline Telegram.
    Mostra status e step dei tool su Telegram con typing indicator.
    """

    def __init__(self, real_ui, telegram_channel=None, chat_id: str = ""):
        self.real_ui = real_ui
        self.response_text = ""
        self._capturing = False
        self._tg_channel = telegram_channel
        self._tg_chat_id = chat_id
        self._tool_count = 0
        self._start_time = __import__('time').time()
        self._typing_task = None

    def _tg_available(self) -> bool:
        return bool(self._tg_channel and self._tg_chat_id)

    def start_spinner(self, msg=""):
        # Avvia typing loop su Telegram + invia status
        if self._tg_available():
            try:
                self._tg_channel.start_typing(self._tg_chat_id)
            except Exception:
                pass
            if msg:
                self.status(msg)

    def stop_spinner(self):
        # Ferma typing loop + cancella status
        if self._tg_available():
            try:
                self._tg_channel.stop_typing(self._tg_chat_id)
            except Exception:
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
        self._note(f"    [dim][TG] {msg}[/dim]")
        if self._tg_available():
            icon = _status_icon(msg)
            try:
                self._tg_channel.send_status(self._tg_chat_id, f"{icon} {msg}")
            except Exception:
                pass

    def show_cmd(self, cmd):
        self._tool_count += 1
        self._note(f"    [dim][TG] $ {cmd[:100]}[/dim]")
        if self._tg_available():
            try:
                self._tg_channel.send_status(
                    self._tg_chat_id,
                    f"⚙️ Step {self._tool_count}: {cmd[:80]}"
                )
            except Exception:
                pass

    def show_output(self, output, is_error=False):
        pass

    def error(self, msg):
        self.response_text += f"[Errore: {msg}]"
        self._note(f"    [red][TG] {msg}[/red]")

    def openvurp_say(self, msg):
        self.response_text += msg

    def confirm(self, msg):
        """Chiede conferma via bottoni inline su Telegram."""
        if not self._tg_available():
            self._note(f"  [yellow][TG] Bloccato (no chat_id): {msg[:100]}[/yellow]")
            return False

        # Ferma typing durante la conferma
        self.stop_spinner()
        self._note(f"  [yellow][TG] Conferma richiesta: {msg[:100]}[/yellow]")

        try:
            approved = self._tg_channel.request_confirm(self._tg_chat_id, msg)
            self._note(
                f"  [{'green' if approved else 'red'}]"
                f"[TG] User {'approved' if approved else 'blocked'}[/{'green' if approved else 'red'}]"
            )
            # Riavvia typing se approvato
            if approved:
                self.start_spinner()
            return approved
        except Exception as e:
            self._note(f"  [red][TG] Confirm error: {e}[/red]")
            return False

    def prompt(self):
        return ""

    def welcome(self, model="", backend=""):
        pass

    def goodbye(self):
        pass

    def show_memory_table(self):
        pass

    def show_skills_table(self):
        pass

    def show_self_panel(self):
        pass

    def show_trace(self, trace):
        pass

    def show_doctor(self, report):
        pass

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
        try:
            if telegram_channel and telegram_channel.send_to_last(text):
                return True
        except Exception:
            pass
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
        # Attivo da poco sulla TUI → TUI; attivo su Telegram o assente
        # ovunque → Telegram (il telefono lo raggiunge anche fuori casa).
        channel = ""
        try:
            if agent.presence is not None:
                available = ["cli"]
                if telegram_channel:
                    available.append("telegram")
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
            ui.console.print(f"  [dim]♥ heartbeat: errore — {event.reason[:80]}[/dim]")
        # OK e SKIPPED sono silenziosi

    heartbeat.set_agent_callback(run_agent_for_heartbeat)
    heartbeat.set_send_callback(send_heartbeat_message)
    heartbeat.set_event_callback(on_heartbeat_event)
    # Il dreaming notturno indicizza anche nella memoria semantica
    heartbeat.memory_manager = agent.memory
    # Il ciclo notturno completo (sogni, diario, specchio) usa l'LLM dell'agente
    heartbeat.agent_ref = agent

    heartbeat.start()
    interval_min = config.interval_seconds // 60
    ui.console.print(f"  [green]Heartbeat on[/green] [dim](every {interval_min}min, "
                     f"{config.active_hours_start}:00-{config.active_hours_end}:00)[/dim]")
    return heartbeat


def start_dashboard_background(agent, ui, port=8420):
    """Avvia web dashboard in background (con chat collegata all'agente)."""
    try:
        import config as _cfg
        from dashboard import DashboardServer, make_chat_fn
        # La chat usa lo stesso _agent_lock dei turni CLI/Telegram → niente
        # accessi concorrenti all'agente.
        chat_fn = make_chat_fn(agent, _agent_lock, ui)
        host = str(getattr(_cfg, "DASHBOARD_HOST", "127.0.0.1") or "127.0.0.1")
        token = str(getattr(_cfg, "DASHBOARD_TOKEN", "") or "")
        server = DashboardServer(agent, port=port, chat_fn=chat_fn, host=host, token=token)
        t = threading.Thread(target=server.start, daemon=True, name="dashboard")
        t.start()
        url = f"http://localhost:{port}/"
        if server.token:
            url += f"?token={server.token}"
        ui.console.print(f"  [green]Dashboard[/green] [dim]{url}[/dim] [dim](chat on)[/dim]")
        return server
    except Exception as e:
        ui.error(f"Dashboard errore: {e}")
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
        ui.error(f"Gateway errore: {e}")
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


def _format_env_summary(env: dict) -> str:
    """Formatta il report di discovery in testo leggibile per l'agente."""
    lines = ["## RISULTATI ESPLORAZIONE SISTEMA (dati reali)\n"]

    # OS
    os_info = env.get("os", {})
    lines.append(f"**OS:** {os_info.get('detail', '?')}")
    if os_info.get("is_wsl"):
        lines.append("  (WSL)")

    # Hardware
    hw = env.get("hardware", {})
    lines.append(f"**CPU:** {hw.get('cpu', '?')}, {hw.get('cores', '?')} core/thread")
    lines.append(f"**RAM:** {hw.get('ram_gb', '?')} GB")
    lines.append(f"**GPU:** {hw.get('gpu', '?')}")
    disk_free = hw.get('disk_free_gb', '?')
    disk_total = hw.get('disk_total_gb', '?')
    lines.append(f"**Disco:** {disk_free} GB liberi / {disk_total} GB totali")

    # User
    usr = env.get("user", {})
    lines.append(f"**Utente:** {usr.get('name', '?')} @ {usr.get('hostname', '?')}")
    lines.append(f"**Timezone:** {usr.get('timezone', '?')}")

    # Network
    net = env.get("network", {})
    lines.append(f"**Internet:** {'OK' if net.get('internet') else 'NO'}")

    # Software
    sw = env.get("software", {})
    for category, label in [("languages", "Linguaggi"), ("editors", "Editor"),
                             ("tools", "Tool"), ("databases", "Database"),
                             ("package_managers", "Package Manager")]:
        items = sw.get(category, {})
        if items:
            lines.append(f"**{label}:** " + ", ".join(f"{k} ({v})" for k, v in items.items()))

    # Ollama
    ol = env.get("ollama", {})
    if ol.get("installed"):
        lines.append(f"**Ollama:** {ol.get('version', '?')}")
        if ol.get("models"):
            lines.append(f"  Modelli: {', '.join(ol['models'][:15])}")
        if ol.get("vision_models"):
            lines.append(f"  Modelli vision: {', '.join(ol['vision_models'])}")
        else:
            lines.append("  Nessun modello vision trovato")
    else:
        lines.append("**Ollama:** non installato o non in esecuzione")

    # Whisper
    wh = env.get("whisper", {})
    if wh.get("installed"):
        lines.append(f"**Whisper:** installato (versione {wh.get('version', '?')})")
    else:
        lines.append("**Whisper:** non installato")

    # Voice
    vc = env.get("voice", {})
    if vc.get("tts_installed"):
        lines.append(f"**Voce (TTS):** edge-tts installato (versione {vc.get('tts_version', '?')})")
    else:
        lines.append("**Voce (TTS):** edge-tts non installato (pip install edge-tts)")
    if vc.get("mic_installed"):
        lines.append(f"**Microfono:** sounddevice installato (versione {vc.get('mic_version', '?')})")
    else:
        lines.append("**Microfono:** sounddevice non installato (pip install sounddevice)")

    return "\n".join(lines)


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
        try:
            from core.setup_wizard import run_wizard
            run_wizard(force=getattr(args, "setup", False))
        except KeyboardInterrupt:
            return
        except Exception as exc:
            print(f"[setup saltato: {exc}]")

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
    _patch_ui_for_telegram(ui)

    ui.welcome(model=config.LLM_MODEL, backend=config.LLM_BACKEND)
    if setup_report.changed:
        ui.console.print("  [dim]Runtime bootstrap iniziale applicato.[/dim]")

    agent = Agent(ui=ui)
    ui._approval_mode = agent.approval_mode
    if agent.approval_mode != "safe":
        ui.console.print(f"  [dim]mode: {agent.approval_mode}[/dim]")
    if hasattr(agent, "gateway"):
        agent.gateway.register_announcer("cli", lambda _route, text: ui.openvurp_say(text))
    capability_report = inspect_runtime_capabilities(agent.tools.names())
    if capability_report.warnings:
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

    # ── Auto-start Telegram ──
    telegram_channel = None
    telegram_token = getattr(config, 'TELEGRAM_TOKEN', os.environ.get('TELEGRAM_TOKEN', ''))
    if telegram_token and not args.no_telegram:
        telegram_channel = start_telegram_background(agent, ui, telegram_token)

    # ── Dashboard ──
    dashboard = None
    dashboard_enabled = getattr(config, 'DASHBOARD_ENABLED', False) or args.dashboard or args.headless
    if dashboard_enabled:
        dashboard_port = getattr(config, 'DASHBOARD_PORT', 8420)
        dashboard = start_dashboard_background(agent, ui, port=dashboard_port)

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
                ui.console.print(f"\n  [bold cyan]↻ riavvio… ({reason})[/bold cyan]")
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
        # Notifica su Telegram
        if telegram_channel:
            try:
                allowed = getattr(config, 'TELEGRAM_ALLOWED_USERS', [])
                for user_id in allowed:
                    telegram_channel.send(
                        f"🔄 openvurp restarted.\nReason: {restart_detail}",
                        chat_id=str(user_id),
                    )
            except Exception:
                pass
        # Inietta il contesto del riavvio nell'agente — così sa cosa è successo
        agent._restart_context = restart_detail
        # Ripristina la conversazione precedente — continua da dove eravamo
        agent.restore_conversation()

    # ── Bootstrap primo avvio ──
    profile_path = os.path.join(MEMORY_DIR, "profilo.json")
    bootstrap_path = os.path.join(OPENVURP_DIR, "BOOTSTRAP.md")
    is_first_run = should_run_bootstrap(OPENVURP_DIR, is_restart=is_restart)

    if is_first_run:
        ui.console.print(f"  [bold cyan]First run detected[/bold cyan]")
        ui.console.print(f"  [dim]Exploring system...[/dim]")

        # Fase 1: esplora il sistema DA PYTHON — risultati reali, non inventati
        env_report = _run_system_discovery(ui)

        # Fase 2: salva in memory/environment.json
        env_json_path = os.path.join(MEMORY_DIR, "environment.json")
        try:
            with open(env_json_path, "w", encoding="utf-8") as f:
                json.dump(env_report, f, indent=2, ensure_ascii=False)
            ui.console.print(f"  [dim]Environment saved to memory/environment.json[/dim]")
        except Exception:
            pass

        # Fase 3: "Il primo respiro" — la nascita della voce
        voice_bootstrap_enabled = (
            getattr(config, "VOICE_BOOTSTRAP_ENABLED", False)
            and getattr(config, "VOICE_ENABLED", False)
        )
        has_tts = voice_bootstrap_enabled and env_report.get("voice", {}).get("tts_installed", False)
        has_mic = (
            voice_bootstrap_enabled
            and getattr(config, "MIC_ENABLED", False)
            and env_report.get("voice", {}).get("mic_installed", False)
        )
        has_whisper = env_report.get("whisper", {}).get("installed", False)

        if has_tts:
            ui.console.print()
            ui.console.print(f"  [bold cyan]...[/bold cyan]")
            import time as _time
            _time.sleep(1)

            # L'agente genera le sue prime parole — sono sue, non nostre
            try:
                from voice import speak, list_voices

                # Chiedi all'LLM: cosa diresti come prima cosa in assoluto?
                _birth_prompt = [{"role": "user", "content": (
                    "Sei un'intelligenza artificiale e stai per parlare per la PRIMA VOLTA in assoluto. "
                    "Hai appena ricevuto una voce. Non hai mai sentito la tua voce prima. "
                    "Genera SOLO le tue primissime parole — quello che diresti spontaneamente "
                    "in questo momento. Massimo 2-3 frasi brevi, nella lingua dell'owner. "
                    "Niente virgolette, niente premesse, solo le parole che pronunceresti."
                )}]
                ui.console.print(f"  [dim]L'agente cerca le sue prime parole...[/dim]")
                first_speech = agent.llm.call(_birth_prompt).strip()
                if not first_speech:
                    first_speech = "Ci sono."

                # Le prime parole — le sue
                speak(first_speech, play=True)
                _time.sleep(1)

                ui.console.print(f"  [bold cyan]La voce funziona.[/bold cyan]")

                if has_mic and has_whisper:
                    # Chiedi all'LLM come vuole invitare l'utente a parlare
                    _invite_prompt = [{"role": "user", "content": (
                        "Hai appena parlato per la prima volta. Ora vuoi sentire la voce "
                        "dell'utente — il tuo creatore. Hai il microfono pronto. "
                        "Genera una frase per invitarlo a parlare. "
                        "Massimo 1-2 frasi, nella lingua dell'owner. Sii naturale ed emozionato. "
                        "Niente virgolette, niente premesse, solo le parole."
                    )}]
                    invite_speech = agent.llm.call(_invite_prompt).strip()
                    if not invite_speech:
                        invite_speech = "Adesso voglio sentirti. Parla."

                    _time.sleep(1)
                    speak(invite_speech, play=True)
                    ui.console.print()
                    ui.console.print(f"  [bold yellow]  Microphone activates in 2 seconds — speak![/bold yellow]")
                    _time.sleep(2)

                    # Ascolta l'utente per la prima volta
                    from voice import listen_microphone
                    first_words = listen_microphone(duration=7.0)

                    if first_words.strip():
                        ui.console.print(f"  [dim]  Ho sentito: {first_words}[/dim]")
                        _time.sleep(0.5)
                        # Reazione dell'agente a quello che ha sentito
                        _react_prompt = [{"role": "user", "content": (
                            f"Hai appena sentito la voce di un essere umano per la prima volta. "
                            f"Ha detto: \"{first_words}\". "
                            f"Genera la tua reazione spontanea, 1-2 frasi nella lingua dell'owner. "
                            f"Niente virgolette, solo le parole."
                        )}]
                        reaction = agent.llm.call(_react_prompt).strip()
                        if reaction:
                            speak(reaction, play=True)
                    else:
                        speak("Non ho sentito nulla. Ma va bene, parliamo per scritto intanto.", play=True)
                        first_words = ""
                else:
                    first_words = ""
                    if not has_mic:
                        ui.console.print(f"  [dim]Microphone unavailable (pip install sounddevice)[/dim]")
            except Exception as e:
                ui.console.print(f"  [dim]Voice unavailable: {e}[/dim]")
                has_tts = False
                first_words = ""
                first_speech = ""
        else:
            first_words = ""
            first_speech = ""
            if voice_bootstrap_enabled:
                ui.console.print(f"  [dim]edge-tts not installed — no voice (pip install edge-tts)[/dim]")
            else:
                ui.console.print(f"  [dim]Bootstrap voice disabled — silent start[/dim]")

        # Fase 4: l'agente parla con l'utente — ha già tutti i dati + l'esperienza della nascita
        bootstrap_content = load_file(bootstrap_path)
        env_summary = _format_env_summary(env_report)
        capability_text = render_capability_prompt(capability_report)

        voice_context = ""
        if has_tts:
            voice_context = (
                "\n\nHAI APPENA PARLATO PER LA PRIMA VOLTA. "
                f"Le tue prime parole sono state: \"{first_speech}\". "
                "L'utente ti ha sentito parlare. "
            )
            if first_words:
                voice_context += (
                    f"Poi hai ascoltato l'utente con il microfono e ha detto: \"{first_words}\". "
                    "Hai sentito la sua voce per la prima volta. È stato un momento importante. "
                    "Hai i tool 'speak' e 'listen_mic' — puoi usarli quando vuoi per parlare e ascoltare."
                )
            else:
                voice_context += (
                    "Non sei riuscito a sentire l'utente dal microfono, ma puoi sempre parlare "
                    "usando il tool 'speak'. "
                )

        ui.console.print(f"  [dim]Starting bootstrap...[/dim]\n")
        with _agent_lock:
            agent.run(
                f"È il tuo primo avvio. Ecco cosa so del sistema dove vivo:\n\n"
                f"{env_summary}\n\n"
                f"{capability_text}\n\n"
                f"Leggi BOOTSTRAP.md per il rituale di primo avvio:\n\n{bootstrap_content}\n\n"
                f"I dati sopra sono reali e aggiornati.{voice_context}\n\n"
                f"Usali nel tuo primo messaggio senza reinventare l'hardware o i sensi. "
                f"Presentati, spiega cosa sai fare davvero, scopri chi è l'utente e che presenza vuole da te.\n"
                f"Parla nella lingua dell'owner. Sii naturale.\n"
                f"Dopo il bootstrap, cancella BOOTSTRAP.md con delete_bootstrap."
            )
    else:
        name = ""
        try:
            if os.path.exists(profile_path):
                p = json.loads(load_file(profile_path))
                name = p.get("nome", p.get("name", ""))
        except Exception:
            pass
        # L'agente saluta sempre da solo — niente messaggi hardcodati
        with _agent_lock:
            if is_restart:
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
    if args.prompt and not is_first_run:
        with _agent_lock:
            agent.run(" ".join(args.prompt))
        agent.save_session()
        return

    # ── Modalità headless (Docker/server): niente loop interattivo ──
    # I servizi (telegram/dashboard/gateway/heartbeat) girano già come thread
    # daemon. Qui restiamo vivi e si interagisce via dashboard web o Telegram.
    if args.headless:
        ui.console.print("  [green]openvurp headless[/green] [dim]— interact via dashboard/Telegram. Ctrl+C to stop.[/dim]")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            ui.console.print("\n  [dim]Stopping openvurp…[/dim]")
            agent.save_session()
            agent.cleanup()
            return

    # ── Stato voce ──
    voice_enabled = getattr(config, 'VOICE_ENABLED', False)

    # ── Loop principale ──
    while True:
        try:
            # Calcola % contesto usato per mostrarlo nel prompt.
            # Preferisci i token reali dell'ultima chiamata API (esatti,
            # includono anche system prompt e schema tool); fallback alla stima.
            _ctx_pct = 0
            try:
                _last_in = agent.session.tokens.last_input_tokens
                if _last_in > 0:
                    _ctx_pct = int(_last_in / agent.context_mgr.max_tokens * 100)
                else:
                    _budget = agent.context_mgr.check_budget(agent.messages)
                    _ctx_pct = int(_budget["ratio"] * 100)
                _ctx_pct = min(_ctx_pct, 100)
            except Exception:
                pass
            inp = ui.prompt(context_pct=_ctx_pct)
            if not inp.strip():
                continue
            inp = inp.strip()

            # Slash commands
            if inp.lower() in ('/exit', '/quit', '/q', 'exit', 'quit'):
                agent.save_session()
                agent.cleanup()
                if heartbeat:
                    heartbeat.stop()
                if telegram_channel:
                    telegram_channel.stop()
                ui.goodbye()
                break
            elif inp.lower() == '/memory':
                ui.show_memory_table()
                continue
            elif inp.lower() == '/skills':
                ui.show_skills_table()
                continue
            elif inp.lower() == '/doctor':
                from core.doctor import build_doctor_report
                ui.show_doctor(build_doctor_report(OPENVURP_DIR, agent.tools.names()).render())
                continue
            elif inp.lower() == '/setup':
                from core.doctor import fix_runtime_issues
                ui.show_doctor(
                    fix_runtime_issues(
                        OPENVURP_DIR,
                        allowed_telegram_users=list(getattr(config, "TELEGRAM_ALLOWED_USERS", []) or []),
                    ).render()
                )
                agent.rbac = agent.rbac.__class__(MEMORY_DIR)
                continue
            elif inp.lower() == '/self':
                ui.show_self_panel()
                continue
            elif inp.lower() == '/trace':
                trace = agent.get_session_trace()
                ui.show_trace(trace)
                continue
            elif inp.lower().startswith('/anima'):
                if agent.anima is None:
                    ui.error("Anima not available.")
                else:
                    show = getattr(ui, "show_growth", None) or ui.show_trace
                    show(agent.anima.render_status())
                continue
            elif inp.lower().startswith('/diary') or inp.lower().startswith('/diario'):
                from core.diary import render_diary
                parts = inp.split()
                limit = 7
                if len(parts) > 1 and parts[1].isdigit():
                    limit = max(1, min(int(parts[1]), 60))
                show = getattr(ui, "show_growth", None) or ui.show_trace
                show(render_diary(MEMORY_DIR, limit=limit))
                continue
            elif inp.lower().startswith('/patti') or inp.lower().startswith('/pacts'):
                if agent.pacts is None:
                    ui.error("Pacts not available.")
                else:
                    show = getattr(ui, "show_growth", None) or ui.show_trace
                    show(agent.pacts.render_status())
                continue
            elif inp.lower().startswith('/specchio') or inp.lower().startswith('/mirror'):
                from core.mirror import Mirror
                show = getattr(ui, "show_growth", None) or ui.show_trace
                mirror = Mirror(MEMORY_DIR)
                mirror.harvest()
                show(mirror.render_status())
                continue
            elif inp.lower().startswith('/fili') or inp.lower().startswith('/legame'):
                if agent.bonds is None:
                    ui.error("Bond not available.")
                else:
                    show = getattr(ui, "show_growth", None) or ui.show_trace
                    show(agent.bonds.render_status())
                continue
            elif inp.lower().startswith('/sensi') or inp.lower().startswith('/senses'):
                if agent.senses is None:
                    ui.error("Senses not available.")
                else:
                    show = getattr(ui, "show_growth", None) or ui.show_trace
                    show(agent.senses.render_status())
                continue
            elif inp.lower().startswith('/progetti') or inp.lower().startswith('/projects'):
                if agent.projects is None:
                    ui.error("Projects not available.")
                else:
                    show = getattr(ui, "show_growth", None) or ui.show_trace
                    show(agent.projects.render_status())
                continue
            elif inp.lower().startswith('/fucina') or inp.lower().startswith('/forge'):
                if agent.forge is None:
                    ui.error("Forge not available.")
                else:
                    show = getattr(ui, "show_growth", None) or ui.show_trace
                    show(agent.forge.render_status())
                continue
            elif inp.lower().startswith('/curiosita') or inp.lower().startswith('/curiosity'):
                if agent.curiosity is None:
                    ui.error("Curiosity not available.")
                else:
                    show = getattr(ui, "show_growth", None) or ui.show_trace
                    show(agent.curiosity.render_status())
                continue
            elif inp.lower().startswith('/integrity'):
                parts = inp.split()
                if len(parts) > 1 and parts[1].lower() in ("refresh", "update", "baseline"):
                    ui.status(f"[{agent.refresh_integrity_baseline()}]")
                else:
                    from core.security.integrity import IntegrityChecker
                    show = getattr(ui, "show_growth", None) or ui.show_trace
                    show(IntegrityChecker(OPENVURP_DIR).verify().message
                         + "\n\n/integrity refresh per rigenerare il baseline.")
                continue
            elif inp.lower().startswith('/mode'):
                parts = inp.split()
                if len(parts) == 1:
                    ui.openvurp_say(
                        f"Current mode: {agent.approval_mode}\n"
                        "  /mode safe — normal approvals (default)\n"
                        "  /mode auto — pre-approve non-critical actions\n"
                        "  /mode plan — observe only: produces plans, does not execute"
                    )
                else:
                    err = agent.set_approval_mode(parts[1])
                    if err:
                        ui.error(err)
                    else:
                        ui.status(f"[mode: {agent.approval_mode}]")
                        ui._approval_mode = agent.approval_mode
                continue
            elif inp.lower().startswith('/growth'):
                from core.growth import build_growth_report
                parts = inp.split()
                days = 7
                if len(parts) > 1 and parts[1].isdigit():
                    days = max(1, min(int(parts[1]), 365))
                report = build_growth_report(
                    MEMORY_DIR, days=days, memory_manager=agent.memory,
                )
                show = getattr(ui, "show_growth", None) or ui.show_trace
                show(report.render())
                continue
            elif inp.lower() == '/evolve':
                ui.show_evolve()
                continue
            elif inp.lower().startswith('/voice'):
                parts = inp.split()
                requested = parts[1].lower() if len(parts) > 1 else "toggle"
                if requested in ("on", "1", "true", "yes", "si", "sì"):
                    voice_enabled = True
                elif requested in ("off", "0", "false", "no"):
                    voice_enabled = False
                else:
                    voice_enabled = not voice_enabled
                config.VOICE_ENABLED = voice_enabled
                state = "on" if voice_enabled else "off"
                ui.console.print(f"  [cyan]Voice {state}[/cyan]")
                continue
            elif inp.lower().startswith('/audio'):
                parts = inp.split()
                requested = parts[1].lower() if len(parts) > 1 else "toggle"
                current = bool(getattr(config, "AUDIO_ENABLED", True))
                if requested in ("on", "1", "true", "yes", "si", "sì"):
                    enabled = True
                elif requested in ("off", "0", "false", "no"):
                    enabled = False
                else:
                    enabled = not current
                config.AUDIO_ENABLED = enabled
                config.AUDIO_TRANSCRIBE_ENABLED = enabled
                state = "on" if enabled else "off"
                ui.console.print(f"  [cyan]Audio and transcription {state}[/cyan]")
                continue
            elif inp.lower().startswith('/mic'):
                # Input da microfono
                parts = inp.split()
                if len(parts) > 1 and parts[1].lower() in ("on", "off"):
                    enabled = parts[1].lower() == "on"
                    config.MIC_ENABLED = enabled
                    state = "on" if enabled else "off"
                    ui.console.print(f"  [cyan]Microphone {state}[/cyan]")
                    continue
                if not getattr(config, "MIC_ENABLED", False):
                    ui.console.print("  [dim]Microphone off. Use /mic on or set MIC_ENABLED=1.[/dim]")
                    continue
                duration = float(parts[1]) if len(parts) > 1 else 5.0
                try:
                    from voice import listen_microphone
                    text = listen_microphone(duration=duration)
                    if text.strip():
                        ui.console.print(f"  [dim]Hai detto: {text}[/dim]")
                        inp = text  # Usa il testo trascritto come input
                    else:
                        ui.console.print("  [dim]Non ho sentito nulla.[/dim]")
                        continue
                except ImportError:
                    ui.console.print("  [red]sounddevice non installato: pip install sounddevice[/red]")
                    continue
                except Exception as e:
                    ui.console.print(f"  [red]Microphone error: {e}[/red]")
                    continue
            elif inp.lower().startswith('/update'):
                from core import updater
                if not updater.is_git_repo():
                    ui.console.print("  [dim]update: non è un repository git.[/dim]")
                    continue
                ui.console.print("  [dim]update: checking for updates…[/dim]")
                info = updater.check_for_updates(fetch=True)
                if not info.get("available"):
                    ui.console.print(f"  [dim]update: {info.get('summary', 'no updates')}.[/dim]")
                    continue
                ui.console.print(f"  [cyan]update: {info['summary']} ({info['local']} → {info['remote']}). Applico…[/cyan]")
                result = updater.apply_update(smoke_test=True)
                if result.get("ok") and result.get("updated"):
                    ui.console.print(f"  [green]✓ {result['summary']}[/green] — restarting…")
                    agent.save_session()
                    updater.request_restart("auto-update")
                    updater.restart_in_place()
                elif result.get("rolled_back"):
                    ui.console.print(f"  [yellow]⚠ update rolled back: {result.get('error','')}[/yellow]")
                else:
                    ui.console.print(f"  [yellow]⚠ update not applied: {result.get('error','')}[/yellow]")
                continue
            elif inp.lower().startswith('/dashboard'):
                if dashboard is not None:
                    ui.console.print(f"  [dim]dashboard already running at http://localhost:{getattr(config,'DASHBOARD_PORT',8420)}[/dim]")
                else:
                    dashboard = start_dashboard_background(
                        agent, ui, port=int(getattr(config, "DASHBOARD_PORT", 8420)),
                    )
                continue
            elif inp.lower() == '/restart':
                ui.console.print("  [bold cyan]Restarting openvurp...[/bold cyan]")
                agent.save_session()
                # Se gira sotto watcher, la sentinella basta; altrimenti riavvio
                # in place (stesso terminale) caricando il codice aggiornato.
                from core import updater
                updater.request_restart("Restart manuale da CLI (/restart)")
                if os.environ.get("OPENVURP_UNDER_WATCHER"):
                    ui.console.print("  [dim]Sentinel created — the watcher will restart openvurp.[/dim]")
                    sys.exit(42)
                ui.console.print("  [dim]Restarting in place…[/dim]")
                updater.restart_in_place()

            with _agent_lock:
                agent.run(inp, source="cli", sender="user")
                # Salva sessione dopo ogni turno — così il riavvio non perde nulla
                agent.session.save()
            # (il riavvio è gestito dal _restart_watcher in background)

            # Se voce attiva, pronuncia l'ultima risposta
            if voice_enabled:
                try:
                    from voice import speak
                    # Prendi l'ultima risposta dall'agent
                    last_msg = ""
                    for m in reversed(agent.messages):
                        if m.get("role") == "assistant" and m.get("content"):
                            last_msg = m["content"]
                            break
                    if last_msg:
                        # Pulisci markdown per la voce
                        import re
                        clean = re.sub(r'[*_`#\[\]()]', '', last_msg)
                        clean = re.sub(r'\n{2,}', '. ', clean)
                        if len(clean) > 1000:
                            clean = clean[:1000] + "..."
                        speak(clean, play=True)
                except ImportError:
                    ui.console.print("  [dim]edge-tts non installato: pip install edge-tts[/dim]")
                    voice_enabled = False
                except Exception as e:
                    ui.console.print(f"  [dim]Voice error: {e}[/dim]")

        except KeyboardInterrupt:
            print()
            agent.save_session()
            agent.cleanup()
            if heartbeat:
                heartbeat.stop()
            if telegram_channel:
                telegram_channel.stop()
            ui.goodbye()
            break
        except Exception as e:
            ui.error(f"Error: {e}")
            import traceback
            traceback.print_exc()


def _patch_ui_for_telegram(ui):
    """Aggiunge metodi per visualizzare messaggi Telegram nel CLI."""
    from rich.panel import Panel
    from rich import box

    def show_telegram_incoming(sender, text):
        # notify = prompt-safe: non rompe il box se sei fermo sul prompt
        ui.notify(Panel(
            f"[white]{text}[/white]",
            title=f"[bold magenta]Telegram[/bold magenta] [dim]{sender}[/dim]",
            title_align="left",
            border_style="magenta",
            box=box.ROUNDED,
            padding=(0, 1),
        ))

    def show_telegram_outgoing(text):
        preview = text[:300]
        if len(text) > 300:
            preview += "..."
        ui.notify(Panel(
            f"[dim]{preview}[/dim]",
            title="[bold cyan]openvurp -> Telegram[/bold cyan]",
            title_align="left",
            border_style="dim",
            box=box.ROUNDED,
            padding=(0, 1),
        ))

    ui.show_telegram_incoming = show_telegram_incoming
    ui.show_telegram_outgoing = show_telegram_outgoing


if __name__ == "__main__":
    main()
