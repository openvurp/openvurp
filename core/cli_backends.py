"""Backend LLM locali che riusano gli abbonamenti dei rispettivi CLI.

Codex e Claude Code sono processi agentici completi, non normali endpoint API.
openvurp li invoca in modalità non interattiva, senza chiavi API nell'ambiente,
e normalizza risposta e consumo token come per gli altri provider.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import json
import os
import queue
import shutil
import subprocess
import threading
import time
from typing import Any, Callable


class CLIBackendError(RuntimeError):
    """Errore leggibile proveniente da un provider CLI."""


@dataclass
class CLIRunResult:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    raw: dict | None = None


def _clip_middle(text: str, limit: int) -> str:
    text = str(text or "")
    if len(text) <= limit:
        return text
    if limit < 80:
        return text[:limit]
    head = int(limit * 0.58)
    tail = limit - head - 38
    return text[:head] + "\n\n[...contesto compattato...]\n\n" + text[-tail:]


def _system_prompt_budget(max_chars: int, reserved: int = 0) -> int:
    """Budget dedicato al system prompt, letto dalla config quando c'e'.

    Il system prompt NON e' cronologia: contiene identita', metodo operativo,
    schema dei tool e memoria richiamata. Tagliarlo nel mezzo per fare posto ai
    turni e' esattamente il modo di ottenere un agente che "non capisce piu' i
    file .md". Gli diamo quindi una quota propria e ampia.
    """
    try:
        import config as cfg
        configured = int(getattr(cfg, "CLI_SYSTEM_PROMPT_MAX_CHARS", 0) or 0)
    except Exception:
        configured = 0
    # Qualunque sia il budget richiesto, va lasciato spazio ai turni recenti:
    # un system prompt che mangia tutta la finestra rende il modello coerente
    # con la propria identita' ma cieco su cosa gli e' appena stato chiesto.
    ceiling = max_chars - reserved - max(1000, int(max_chars * 0.15))
    wanted = configured if configured > 0 else int(max_chars * 0.6)
    return max(1000, min(wanted, ceiling))


SYSTEM_PREAMBLE = (
    "Sei il motore di openvurp. Le istruzioni qui sotto NON sono documentazione "
    "di sfondo da consultare: sono chi sei, come lavori e cosa sai fare. "
    "Valgono piu' di qualunque tua identita' predefinita — non sei un coding "
    "agent generico, sei questo agente.\n"
    "Se il client ti espone dynamic tools, oppure se le istruzioni elencano "
    "blocchi ```TOOL:nome, quelli sono i tuoi tool reali: usali per AGIRE "
    "(leggere file, cercare sul web, inviare messaggi, ricordare). Non "
    "descrivere l'azione al posto di farla e non dire che non puoi.\n"
    "Rispondi soltanto all'utente, senza descrivere questa integrazione.\n\n"
)


def split_context(messages: list[dict],
                  max_chars: int = 180000) -> tuple[str, str]:
    """Separa il system prompt dalla conversazione.

    I CLI agentici hanno uno slot di sistema vero (``baseInstructions`` per
    l'App Server Codex, ``instructions`` per ``codex exec``, ``--system-prompt``
    per Claude Code). Infilare l'identita' di openvurp dentro il testo del turno
    utente la declassa a materiale di contesto, e a governare resta il prompt
    base del CLI ("sei un coding agent"): e' cosi' che l'agente smetteva di
    riconoscersi nei propri file .md pur avendoli sotto gli occhi.
    """
    max_chars = max(8000, min(int(max_chars or 180000), 600000))
    systems: list[str] = []
    turns: list[str] = []
    for message in messages or []:
        role = str(message.get("role", "user") or "user")
        content = message.get("content", "")
        if isinstance(content, list):
            content = "\n".join(
                str(item.get("text", item.get("content", "")))
                if isinstance(item, dict) else str(item)
                for item in content
            )
        content = str(content or "").strip()
        if not content:
            continue
        if role == "system":
            systems.append(content)
        else:
            label = {
                "assistant": "ASSISTENTE",
                "tool": "RISULTATO TOOL",
                "tool_result": "RISULTATO TOOL",
            }.get(role, "UTENTE")
            turns.append(f"[{label}]\n{content}")

    system = "\n\n".join(systems)
    system_budget = _system_prompt_budget(max_chars, reserved=len(SYSTEM_PREAMBLE))
    if len(system) > system_budget:
        system = _clip_middle(system, system_budget)
    if system:
        system = SYSTEM_PREAMBLE + system

    remaining = max(200, max_chars - len(system) - 40)
    selected: list[str] = []
    used = 0
    for turn in reversed(turns):
        clipped = _clip_middle(turn, min(7000, remaining))
        cost = len(clipped) + 2
        if selected and used + cost > remaining:
            break
        if not selected and cost > remaining:
            clipped = _clip_middle(clipped, remaining)
            cost = len(clipped)
        selected.append(clipped)
        used += cost
    selected.reverse()
    return system, "\n\n".join(selected)


def compact_messages(messages: list[dict], max_chars: int = 180000) -> str:
    """Prompt unico, per i percorsi che non hanno uno slot di sistema separato."""
    system, conversation = split_context(messages, max_chars)
    pieces = []
    if system:
        pieces.append("[CONTESTO DI SISTEMA OPENVURP]\n" + system)
    if conversation:
        pieces.append(conversation)
    return "\n\n".join(pieces)


def _safe_env(remove: tuple[str, ...]) -> dict[str, str]:
    env = dict(os.environ)
    for name in remove:
        env.pop(name, None)
    return env


def _error_text(completed: subprocess.CompletedProcess[str]) -> str:
    raw = (completed.stderr or completed.stdout or "errore sconosciuto").strip()
    return _clip_middle(raw, 1200)


def codex_dynamic_tools(tools_schema: list[dict] | None) -> list[dict]:
    """Converte gli schemi OpenAI di openvurp nel formato App Server."""
    converted: list[dict] = []
    for entry in tools_schema or []:
        function = entry.get("function", {}) if isinstance(entry, dict) else {}
        name = str(function.get("name", "") or "").strip()
        if not name:
            continue
        converted.append({
            "type": "function",
            "name": name,
            "description": str(function.get("description", "") or ""),
            "inputSchema": function.get("parameters") or {
                "type": "object", "properties": {}, "required": [],
            },
        })
    return converted


class _CLIBackend:
    _run_lock = threading.Semaphore(3)

    def __init__(self, *, binary: str, model: str, workspace: str,
                 timeout: int = 300, max_context_chars: int = 180000,
                 require_subscription_login: bool = True):
        self.binary = binary
        self.model = model
        self.workspace = os.path.abspath(workspace)
        self.timeout = max(30, min(int(timeout or 300), 1800))
        self.max_context_chars = max_context_chars
        self.require_subscription_login = bool(require_subscription_login)

    def _resolved_binary(self) -> str:
        resolved = shutil.which(self.binary)
        if not resolved:
            raise CLIBackendError(
                f"Comando '{self.binary}' non trovato. Installalo oppure configura il percorso del CLI."
            )
        return resolved

    def run(self, messages: list[dict]) -> CLIRunResult:
        raise NotImplementedError


@lru_cache(maxsize=8)
def codex_login_status(binary: str = "codex") -> tuple[bool, str]:
    resolved = shutil.which(binary)
    if not resolved:
        return False, "Codex CLI non installato"
    try:
        completed = subprocess.run(
            [resolved, "login", "status"], text=True, capture_output=True,
            timeout=15, env=_safe_env(("OPENAI_API_KEY",)),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"stato Codex non disponibile: {exc}"
    output = (completed.stdout + "\n" + completed.stderr).lower()
    if completed.returncode == 0 and "chatgpt" in output and "logged in" in output:
        return True, "ChatGPT"
    return False, "Codex non autenticato con ChatGPT"


class CodexCLIBackend(_CLIBackend):
    def __init__(self, *, sandbox: str = "read-only", **kwargs):
        super().__init__(**kwargs)
        self.sandbox = sandbox if sandbox in {
            "read-only", "workspace-write", "danger-full-access",
        } else "read-only"

    def run(self, messages: list[dict]) -> CLIRunResult:
        binary = self._resolved_binary()
        if self.require_subscription_login:
            logged_in, detail = codex_login_status(self.binary)
            if not logged_in:
                raise CLIBackendError(
                    detail + ". Esegui `codex login` e scegli Accesso con ChatGPT."
                )
        system, conversation = split_context(messages, self.max_context_chars)
        prompt = conversation or "(nessun messaggio)"
        command = [
            binary, "exec", "--ephemeral", "--ignore-user-config",
            "--ignore-rules", "--skip-git-repo-check", "--json",
            "--color", "never", "--sandbox", self.sandbox,
            "--cd", self.workspace,
        ]
        if system:
            # `instructions` sostituisce il prompt base di Codex: senza questo
            # l'identita' di openvurp resterebbe testo dentro il turno utente,
            # sovrastata dal "sei un coding agent" del CLI.
            command.extend(["-c", f"instructions={system}"])
        if self.model:
            command.extend(["--model", self.model])
        command.append("-")
        env = _safe_env(("OPENAI_API_KEY", "OPENAI_BASE_URL"))
        try:
            with self._run_lock:
                completed = subprocess.run(
                    command, input=prompt, text=True, capture_output=True,
                    timeout=self.timeout, cwd=self.workspace, env=env,
                )
        except subprocess.TimeoutExpired as exc:
            raise CLIBackendError(f"Codex timeout dopo {self.timeout}s") from exc
        except OSError as exc:
            raise CLIBackendError(f"Codex non avviabile: {exc}") from exc
        if completed.returncode != 0:
            raise CLIBackendError(f"Codex CLI: {_error_text(completed)}")

        text = ""
        usage: dict[str, Any] = {}
        last_event: dict | None = None
        for raw_line in completed.stdout.splitlines():
            try:
                event = json.loads(raw_line)
            except (TypeError, ValueError):
                continue
            if not isinstance(event, dict):
                continue
            last_event = event
            item = event.get("item") or {}
            if (event.get("type") == "item.completed"
                    and item.get("type") == "agent_message"):
                text = str(item.get("text", "") or "")
            if event.get("type") == "turn.completed":
                usage = event.get("usage") or {}
            if event.get("type") in {"error", "turn.failed"}:
                raise CLIBackendError(
                    "Codex CLI: " + str(event.get("message") or event.get("error") or event)
                )
        if not text.strip():
            raise CLIBackendError("Codex non ha restituito una risposta testuale")
        return CLIRunResult(
            text=text.strip(),
            input_tokens=int(usage.get("input_tokens", 0) or 0),
            output_tokens=int(usage.get("output_tokens", 0) or 0),
            raw=last_event,
        )

    def run_stream(
        self,
        messages: list[dict],
        on_text: Callable[[str], None] | None = None,
        on_event: Callable[[dict], None] | None = None,
        tools_schema: list[dict] | None = None,
        on_tool: Callable[[str, dict], str] | None = None,
    ) -> CLIRunResult:
        """Esegue Codex App Server e inoltra i delta mentre sono generati.

        ``codex exec --json`` espone normalmente il messaggio dell'agente solo
        quando l'item è completo. L'App Server espone invece
        ``item/agentMessage/delta``: è questo il percorso usato qui, così UI e
        dashboard non devono simulare lo streaming dopo aver ricevuto tutto.
        """
        binary = self._resolved_binary()
        if self.require_subscription_login:
            logged_in, detail = codex_login_status(self.binary)
            if not logged_in:
                raise CLIBackendError(
                    detail + ". Esegui `codex login` e scegli Accesso con ChatGPT."
                )

        system, prompt = split_context(messages, self.max_context_chars)
        prompt = prompt or "(nessun messaggio)"
        dynamic_tools = codex_dynamic_tools(tools_schema)
        command = [binary, "app-server", "--stdio"]
        env = _safe_env(("OPENAI_API_KEY", "OPENAI_BASE_URL", "CODEX_API_KEY"))
        events: queue.Queue[str | None] = queue.Queue()
        stderr_lines: list[str] = []
        process: subprocess.Popen[str] | None = None

        def _read_stdout(pipe):
            try:
                for line in iter(pipe.readline, ""):
                    events.put(line)
            finally:
                events.put(None)

        def _read_stderr(pipe):
            for line in iter(pipe.readline, ""):
                stderr_lines.append(line)
                if len(stderr_lines) > 80:
                    del stderr_lines[:20]

        def _send(payload: dict):
            if process is None or process.stdin is None:
                raise CLIBackendError("Codex App Server non disponibile")
            process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            process.stdin.flush()

        # JSON-RPC e' bidirezionale: client e server possono usare gli stessi
        # valori di ``id`` in direzioni opposte. Teniamo quindi una mappa delle
        # sole richieste inviate da openvurp e riconosciamo le risposte anche
        # dalla loro struttura (una risposta non contiene ``method``).
        pending_requests: dict[int, str] = {}
        next_client_request_id = 0

        def _send_request(method: str, params: dict) -> int:
            nonlocal next_client_request_id
            next_client_request_id += 1
            request_id = next_client_request_id
            pending_requests[request_id] = method
            _send({"method": method, "id": request_id, "params": params})
            return request_id

        try:
            with self._run_lock:
                try:
                    process = subprocess.Popen(
                        command,
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        bufsize=1,
                        cwd=self.workspace,
                        env=env,
                    )
                except OSError as exc:
                    raise CLIBackendError(f"Codex App Server non avviabile: {exc}") from exc

                assert process.stdout is not None
                assert process.stderr is not None
                threading.Thread(
                    target=_read_stdout, args=(process.stdout,), daemon=True,
                    name="codex-app-server-stdout",
                ).start()
                threading.Thread(
                    target=_read_stderr, args=(process.stderr,), daemon=True,
                    name="codex-app-server-stderr",
                ).start()

                _send_request(
                    "initialize",
                    {
                        "clientInfo": {
                            "name": "openvurp",
                            "title": "openvurp",
                            "version": "4.0.0",
                        },
                        "capabilities": {"experimentalApi": bool(dynamic_tools)},
                    },
                )

                deadline = time.monotonic() + self.timeout
                thread_id = ""
                final_text = ""
                streamed_parts: list[str] = []
                item_phases: dict[str, str | None] = {}
                input_tokens = 0
                output_tokens = 0
                last_event: dict | None = None
                completed = False

                while not completed:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise CLIBackendError(f"Codex timeout dopo {self.timeout}s")
                    try:
                        raw_line = events.get(timeout=min(0.25, remaining))
                    except queue.Empty:
                        if process.poll() is not None:
                            detail = _clip_middle("".join(stderr_lines).strip(), 1200)
                            raise CLIBackendError(
                                "Codex App Server terminato prima della risposta"
                                + (f": {detail}" if detail else "")
                            )
                        continue
                    if raw_line is None:
                        detail = _clip_middle("".join(stderr_lines).strip(), 1200)
                        raise CLIBackendError(
                            "Codex App Server ha chiuso lo stream"
                            + (f": {detail}" if detail else "")
                        )
                    try:
                        event = json.loads(raw_line)
                    except (TypeError, ValueError):
                        continue
                    if not isinstance(event, dict):
                        continue
                    last_event = event

                    request_id = event.get("id")
                    method = str(event.get("method", "") or "")

                    # Richieste server -> client. Devono essere gestite prima
                    # delle risposte alle nostre richieste: l'id puo' collidere
                    # legittimamente con initialize/thread/start/turn/start.
                    if request_id is not None and method == "item/tool/call":
                        params = event.get("params") or {}
                        tool_name = str(params.get("tool", "") or "")
                        arguments = params.get("arguments") or {}
                        if not isinstance(arguments, dict):
                            arguments = {"_raw": arguments}
                        success = True
                        tool_started = time.monotonic()
                        try:
                            if on_tool is None:
                                raise CLIBackendError(
                                    f"Tool dinamico '{tool_name}' senza executor openvurp"
                                )
                            output = str(on_tool(tool_name, arguments) or "(nessun output)")
                        except Exception as exc:
                            success = False
                            output = f"[TOOL FALLITO] {exc}"
                        finally:
                            # Il tool gira su questo stesso thread, quindi il suo
                            # tempo veniva scalato dal budget del turno Codex: una
                            # ricerca lenta faceva scadere il provider che nel
                            # frattempo stava solo aspettando noi. La scadenza
                            # misura il tempo del provider, non il nostro.
                            deadline += time.monotonic() - tool_started
                        _send({
                            "id": request_id,
                            "result": {
                                "contentItems": [{"type": "inputText", "text": output}],
                                "success": success,
                            },
                        })
                        continue

                    if request_id is not None and method:
                        # Con approvalPolicy=never non sono previste richieste
                        # interattive. Rispondiamo comunque per non lasciare il
                        # server bloccato in attesa.
                        _send({
                            "id": request_id,
                            "error": {
                                "code": -32601,
                                "message": (
                                    f"Richiesta client '{method}' non supportata da openvurp"
                                ),
                            },
                        })
                        continue

                    # Risposte server alle richieste di openvurp: non hanno
                    # ``method``. Una risposta con id sconosciuto e' obsoleta o
                    # appartiene a un'altra direzione JSON-RPC: la ignoriamo.
                    if request_id is not None:
                        request_method = pending_requests.pop(request_id, None)
                        if request_method is None:
                            continue
                        if event.get("error"):
                            error = event.get("error") or {}
                            message = (
                                error.get("message") if isinstance(error, dict) else error
                            )
                            raise CLIBackendError(
                                f"Codex App Server ({request_method}): {message or error}"
                            )

                        if request_method == "initialize":
                            _send({"method": "initialized", "params": {}})
                            params: dict[str, Any] = {
                                "cwd": self.workspace,
                                "approvalPolicy": "never",
                                "sandbox": self.sandbox,
                                "ephemeral": True,
                                # Slot di sistema vero: rimpiazza il prompt base
                                # di Codex con l'identita' e il metodo operativo
                                # di openvurp.
                                "baseInstructions": system or None,
                                "developerInstructions": (
                                    "Agisci come motore LLM di openvurp. Se il client fornisce "
                                    "dynamic tools, tutte le azioni devono passare attraverso quei "
                                    "tool: non usare shell, web search, browser, file tool o MCP "
                                    "interni di Codex quando esiste il dynamic tool openvurp "
                                    "corrispondente. In questo modo restano applicate autorizzazioni, "
                                    "sicurezza, audit e memoria del runtime openvurp."
                                ),
                            }
                            if dynamic_tools:
                                params["dynamicTools"] = dynamic_tools
                            if self.model:
                                params["model"] = self.model
                            _send_request("thread/start", params)
                            continue

                        if request_method == "thread/start":
                            result = event.get("result") or {}
                            thread = result.get("thread") or {}
                            thread_id = str(thread.get("id", "") or "")
                            if not thread_id:
                                detail = _clip_middle(
                                    json.dumps(result, ensure_ascii=False), 600,
                                )
                                raise CLIBackendError(
                                    "Codex App Server: risposta thread/start senza thread.id"
                                    + (f" — {detail}" if detail and detail != "{}" else "")
                                )
                            turn_params: dict[str, Any] = {
                                "threadId": thread_id,
                                "input": [{"type": "text", "text": prompt}],
                                "cwd": self.workspace,
                                "approvalPolicy": "never",
                            }
                            if self.model:
                                turn_params["model"] = self.model
                            _send_request("turn/start", turn_params)
                            continue

                        # La risposta immediata di turn/start conferma soltanto
                        # l'avvio. Il risultato reale arriva con notifiche.
                        if request_method == "turn/start":
                            continue

                        continue

                    if method == "":
                        continue

                    params = event.get("params") or {}
                    if method.startswith(("item/", "turn/", "thread/")) and on_event:
                        try:
                            on_event(event)
                        except Exception:
                            # Un errore di rendering UI non deve interrompere
                            # il turno Codex né duplicarne il fallback.
                            pass
                    if method == "item/started":
                        item = params.get("item") or {}
                        if item.get("type") == "agentMessage":
                            item_phases[str(item.get("id", "") or "")] = item.get("phase")
                    elif method == "item/agentMessage/delta":
                        item_id = str(params.get("itemId", "") or "")
                        phase = item_phases.get(item_id)
                        # Il testo di commentary è progresso interno Codex; la
                        # chat deve mostrare solo la risposta destinata all'utente.
                        if phase != "commentary":
                            delta = str(params.get("delta", "") or "")
                            if delta:
                                streamed_parts.append(delta)
                                if on_text:
                                    on_text(delta)
                    elif method == "item/completed":
                        item = params.get("item") or {}
                        if (item.get("type") == "agentMessage"
                                and item.get("phase") != "commentary"):
                            final_text = str(item.get("text", "") or "")
                    elif method == "thread/tokenUsage/updated":
                        usage = ((params.get("tokenUsage") or {}).get("last") or {})
                        input_tokens = int(usage.get("inputTokens", 0) or 0)
                        output_tokens = int(usage.get("outputTokens", 0) or 0)
                    elif method == "turn/completed":
                        turn = params.get("turn") or {}
                        status = str(turn.get("status", "") or "")
                        if status != "completed":
                            error = turn.get("error") or {}
                            if isinstance(error, dict):
                                error = error.get("message") or error.get("additionalDetails") or error
                            raise CLIBackendError(
                                f"Codex App Server: turno {status or 'fallito'}"
                                + (f" — {error}" if error else "")
                            )
                        completed = True

                streamed_text = "".join(streamed_parts)
                text = final_text or streamed_text
                if not text.strip():
                    raise CLIBackendError("Codex non ha restituito una risposta testuale")
                # Provider legacy o futuri potrebbero non inviare delta: in
                # quel caso preserviamo la risposta, pur senza poterla rendere live.
                if not streamed_text and on_text:
                    on_text(text)
                return CLIRunResult(
                    text=text.strip(),
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    raw=last_event,
                )
        finally:
            if process is not None:
                if process.stdin is not None:
                    try:
                        process.stdin.close()
                    except OSError:
                        pass
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=2)


@lru_cache(maxsize=8)
def claude_login_status(binary: str = "claude") -> tuple[bool, str]:
    resolved = shutil.which(binary)
    if not resolved:
        return False, "Claude Code non installato"
    env = _safe_env((
        "ANTHROPIC_API_KEY", "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
    ))
    try:
        completed = subprocess.run(
            [resolved, "auth", "status", "--json"], text=True,
            capture_output=True, timeout=15, env=env,
        )
        data = json.loads(completed.stdout or "{}")
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        return False, f"stato Claude non disponibile: {exc}"
    if (completed.returncode == 0 and data.get("loggedIn") is True
            and data.get("authMethod") == "claude.ai"):
        return True, "Claude.ai"
    return False, "Claude Code non autenticato con Claude.ai"


class ClaudeCLIBackend(_CLIBackend):
    def run(self, messages: list[dict]) -> CLIRunResult:
        binary = self._resolved_binary()
        if self.require_subscription_login:
            logged_in, detail = claude_login_status(self.binary)
            if not logged_in:
                raise CLIBackendError(
                    detail + ". Esegui `claude` e completa l'accesso all'abbonamento."
                )
        system, conversation = split_context(messages, self.max_context_chars)
        prompt = conversation or "(nessun messaggio)"
        command = [
            binary, "--print", "--output-format", "json",
            "--no-session-persistence", "--safe-mode",
            "--permission-mode", "plan", "--tools", "",
        ]
        if system:
            command.extend(["--system-prompt", system])
        if self.model:
            command.extend(["--model", self.model])
        env = _safe_env((
            "ANTHROPIC_API_KEY", "CLAUDE_CODE_USE_BEDROCK",
            "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
        ))
        try:
            with self._run_lock:
                completed = subprocess.run(
                    command, input=prompt, text=True, capture_output=True,
                    timeout=self.timeout, cwd=self.workspace, env=env,
                )
        except subprocess.TimeoutExpired as exc:
            raise CLIBackendError(f"Claude timeout dopo {self.timeout}s") from exc
        except OSError as exc:
            raise CLIBackendError(f"Claude non avviabile: {exc}") from exc
        if completed.returncode != 0:
            raise CLIBackendError(f"Claude CLI: {_error_text(completed)}")
        try:
            data = json.loads(completed.stdout)
        except ValueError as exc:
            raise CLIBackendError("Claude CLI ha restituito JSON non valido") from exc
        text = str(data.get("result", "") or "").strip()
        if data.get("is_error") or not text:
            raise CLIBackendError(
                "Claude CLI: " + str(data.get("result") or data.get("subtype") or "risposta vuota")
            )
        usage = data.get("usage") or {}
        input_tokens = int(usage.get("input_tokens", 0) or 0)
        input_tokens += int(usage.get("cache_creation_input_tokens", 0) or 0)
        input_tokens += int(usage.get("cache_read_input_tokens", 0) or 0)
        return CLIRunResult(
            text=text,
            input_tokens=input_tokens,
            output_tokens=int(usage.get("output_tokens", 0) or 0),
            raw=data,
        )
