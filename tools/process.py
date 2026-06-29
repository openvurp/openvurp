"""
openvurp Tool — Process Management

Gestione processi di sistema e sessioni terminali in background.
"""

from __future__ import annotations

import atexit
from dataclasses import dataclass, field
import errno
import os
import shlex
import signal
import subprocess
import sys
import threading
import time
import uuid

from core.tools import Tool, ToolResult, ErrorType
from core.security.sandbox import Sandbox, SandboxConfig, SandboxMode
from core.runtime_shell import (
    build_shell_command,
    default_allowed_env_vars,
    resolve_effective_shell,
)


MAX_SESSION_OUTPUT_CHARS = 200_000
MAX_BACKGROUND_SESSIONS = 24
FINISHED_SESSION_TTL_SECONDS = 3600

def _build_sandbox_for_process(workdir: str | None) -> Sandbox:
    import config as cfg

    cwd = os.path.abspath(workdir or os.getcwd())
    raw_mode = str(getattr(cfg, "SANDBOX_MODE", "restricted") or "restricted").lower()
    try:
        mode = SandboxMode(raw_mode)
    except ValueError:
        mode = SandboxMode.RESTRICTED

    allowed_paths = list(getattr(cfg, "SANDBOX_ALLOWED_PATHS", []) or [])
    if not allowed_paths:
        allowed_paths = [os.getcwd()]

    resolved_shell = resolve_effective_shell(getattr(cfg, "SHELL", ""))
    sandbox_cfg = SandboxConfig(
        mode=mode,
        shell_executable=resolved_shell.path,
        allowed_paths=[os.path.abspath(path) for path in allowed_paths],
        allowed_env_vars=default_allowed_env_vars(),
    )
    return Sandbox(config=sandbox_cfg, working_dir=cwd)


@dataclass
class ManagedProcessSession:
    id: str
    command: str
    process: subprocess.Popen
    workdir: str = ""
    started_at: float = field(default_factory=time.time)
    last_activity_at: float = field(default_factory=time.time)
    output: str = ""
    dropped_chars: int = 0
    reader_thread: threading.Thread | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)
    use_pty: bool = False
    master_fd: int | None = None

    @property
    def pid(self) -> int:
        return int(getattr(self.process, "pid", 0) or 0)

    @property
    def running(self) -> bool:
        return self.process.poll() is None

    @property
    def returncode(self) -> int | None:
        return self.process.poll()

    def append_output(self, chunk: str):
        if not chunk:
            return
        with self.lock:
            self.output += chunk
            self.last_activity_at = time.time()
            if len(self.output) > MAX_SESSION_OUTPUT_CHARS:
                overflow = len(self.output) - MAX_SESSION_OUTPUT_CHARS
                self.output = self.output[overflow:]
                self.dropped_chars += overflow

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "session_id": self.id,
                "command": self.command,
                "workdir": self.workdir,
                "pid": self.pid,
                "running": self.running,
                "returncode": self.returncode,
                "started_at": self.started_at,
                "last_activity_at": self.last_activity_at,
                "output": self.output,
                "dropped_chars": self.dropped_chars,
                "next_cursor": self.dropped_chars + len(self.output),
            }


class BackgroundProcessManager:
    """Gestisce processi in background con output incrementale."""

    def __init__(self):
        self._sessions: dict[str, ManagedProcessSession] = {}
        self._lock = threading.Lock()

    def start(self, command: str, workdir: str = "") -> ManagedProcessSession:
        if not command.strip():
            raise ValueError("Comando vuoto")

        cwd = os.path.abspath(workdir) if workdir else ""
        if cwd and not os.path.isdir(cwd):
            raise FileNotFoundError(f"Directory non trovata: {cwd}")

        with self._lock:
            self._prune_locked()
            if len(self._sessions) >= MAX_BACKGROUND_SESSIONS:
                raise RuntimeError(
                    f"Limite sessioni background raggiunto ({MAX_BACKGROUND_SESSIONS}). "
                    "Chiudi qualche sessione prima di aprirne altre."
                )

            process, use_pty, master_fd = self._spawn_process(command, cwd or None)
            session_id = str(uuid.uuid4())[:8]
            session = ManagedProcessSession(
                id=session_id,
                command=command,
                process=process,
                workdir=cwd,
                use_pty=use_pty,
                master_fd=master_fd,
            )
            self._sessions[session_id] = session

        reader = threading.Thread(
            target=self._reader_loop,
            args=(session,),
            daemon=True,
            name=f"process-reader-{session_id}",
        )
        session.reader_thread = reader
        reader.start()
        return session

    def list_sessions(self, running_only: bool = False) -> list[dict]:
        with self._lock:
            self._prune_locked()
            sessions = list(self._sessions.values())

        rows = []
        for session in sorted(sessions, key=lambda s: s.started_at, reverse=True):
            if running_only and not session.running:
                continue
            rows.append({
                "session_id": session.id,
                "pid": session.pid,
                "status": "running" if session.running else f"exited({session.returncode})",
                "elapsed_s": int(time.time() - session.started_at),
                "workdir": session.workdir or os.getcwd(),
                "command": session.command,
            })
        return rows

    def read(self, session_id: str, cursor: int = -1, max_chars: int = 4000) -> dict:
        session = self.get(session_id)
        snap = session.snapshot()
        output = snap["output"]
        dropped = snap["dropped_chars"]
        total_cursor = snap["next_cursor"]
        max_chars = max(200, min(int(max_chars or 4000), 20000))

        truncated = False
        notice = ""

        if cursor is None:
            cursor = -1

        if cursor < 0:
            chunk = output[-max_chars:]
            if len(output) > max_chars:
                truncated = True
                notice = "Output recente troncato ai caratteri più recenti."
            cursor_start = max(dropped, total_cursor - len(chunk))
            next_cursor = total_cursor
        else:
            cursor = max(0, int(cursor))
            cursor_start = cursor
            if cursor < dropped:
                notice = "Cursor scaduto: parte dell'output vecchio è stata rimossa."
                truncated = True
                chunk = output[-max_chars:]
                cursor_start = max(dropped, total_cursor - len(chunk))
                next_cursor = total_cursor
            else:
                start = cursor - dropped
                fresh = output[start:]
                chunk = fresh[:max_chars]
                if len(fresh) > max_chars:
                    truncated = True
                    notice = "Output nuovo troncato: richiama process_read con il next_cursor ritornato."
                next_cursor = cursor + len(chunk)

        snap.update({
            "cursor_start": cursor_start,
            "next_cursor": next_cursor,
            "output_chunk": chunk,
            "output_truncated": truncated,
            "notice": notice,
        })
        return snap

    def write(self, session_id: str, text: str, append_newline: bool = True) -> ManagedProcessSession:
        session = self.get(session_id)
        if not session.running:
            raise RuntimeError(f"Session {session_id} is no longer active")

        payload = text
        if append_newline and not payload.endswith("\n"):
            payload += "\n"

        if session.use_pty:
            if session.master_fd is None:
                raise RuntimeError("PTY chiusa")
            os.write(session.master_fd, payload.encode("utf-8", errors="replace"))
        else:
            if not session.process.stdin:
                raise RuntimeError("stdin non disponibile per questa sessione")
            session.process.stdin.write(payload)
            session.process.stdin.flush()

        session.last_activity_at = time.time()
        return session

    def stop(self, session_id: str, force: bool = False, wait_timeout: float = 5.0) -> dict:
        session = self.get(session_id)
        was_running = session.running

        if was_running:
            self._terminate_process(session, force=force)
            try:
                session.process.wait(timeout=max(0.1, float(wait_timeout)))
            except subprocess.TimeoutExpired:
                if not force:
                    self._terminate_process(session, force=True)
                    session.process.wait(timeout=max(0.1, float(wait_timeout)))

        snap = session.snapshot()
        snap["was_running"] = was_running
        return snap

    def get(self, session_id: str) -> ManagedProcessSession:
        with self._lock:
            session = self._sessions.get(session_id)
        if not session:
            raise KeyError(f"Sessione {session_id} non trovata")
        return session

    def stop_all(self):
        with self._lock:
            sessions = list(self._sessions.values())
        for session in sessions:
            try:
                self.stop(session.id, force=True, wait_timeout=1.0)
            except Exception:
                pass

    def _spawn_process(self, command: str, workdir: str | None) -> tuple[subprocess.Popen, bool, int | None]:
        try:
            import config as cfg
            configured_shell = getattr(cfg, "SHELL", "")
        except Exception:
            configured_shell = ""
        resolved_shell = resolve_effective_shell(configured_shell)
        argv = build_shell_command(command, resolved_shell)

        if sys.platform != "win32":
            master_fd = None
            slave_fd = None
            try:
                import pty

                master_fd, slave_fd = pty.openpty()
                kw = {
                    "cwd": workdir,
                    "stdin": slave_fd,
                    "stdout": slave_fd,
                    "stderr": slave_fd,
                    "close_fds": True,
                    "preexec_fn": os.setsid,
                }
                process = subprocess.Popen(argv, **kw)
                os.close(slave_fd)
                return process, True, master_fd
            except Exception:
                try:
                    os.close(master_fd)
                except Exception:
                    pass
                try:
                    os.close(slave_fd)
                except Exception:
                    pass

        kw = {
            "cwd": workdir,
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.STDOUT,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "bufsize": 1,
        }
        if sys.platform == "win32":
            kw["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            kw["preexec_fn"] = os.setsid

        process = subprocess.Popen(argv, **kw)
        return process, False, None

    def _reader_loop(self, session: ManagedProcessSession):
        try:
            if session.use_pty:
                self._read_pty_output(session)
            else:
                self._read_pipe_output(session)
        except Exception as e:
            session.append_output(f"\n[reader error] {e}\n")
        finally:
            if session.use_pty and session.master_fd is not None:
                try:
                    os.close(session.master_fd)
                except OSError:
                    pass
                session.master_fd = None

    def _read_pty_output(self, session: ManagedProcessSession):
        while True:
            try:
                chunk = os.read(session.master_fd, 1024) if session.master_fd is not None else b""
            except OSError as e:
                if e.errno in (errno.EIO, errno.EBADF):
                    break
                raise
            if not chunk:
                if not session.running:
                    break
                time.sleep(0.05)
                continue
            session.append_output(chunk.decode("utf-8", errors="replace"))

    def _read_pipe_output(self, session: ManagedProcessSession):
        stream = session.process.stdout
        if not stream:
            return

        while True:
            chunk = stream.readline()
            if chunk == "" and not session.running:
                break
            if chunk:
                session.append_output(chunk)

        tail = stream.read()
        if tail:
            session.append_output(tail)

    def _terminate_process(self, session: ManagedProcessSession, force: bool = False):
        if not session.running:
            return

        if sys.platform == "win32":
            if force:
                session.process.kill()
            else:
                session.process.terminate()
            return

        sig = signal.SIGKILL if force else signal.SIGTERM
        try:
            os.killpg(os.getpgid(session.process.pid), sig)
        except ProcessLookupError:
            pass

    def _prune_locked(self):
        now = time.time()
        stale = [
            session_id
            for session_id, session in self._sessions.items()
            if (not session.running) and (now - session.last_activity_at > FINISHED_SESSION_TTL_SECONDS)
        ]
        for session_id in stale:
            self._sessions.pop(session_id, None)

        if len(self._sessions) < MAX_BACKGROUND_SESSIONS:
            return

        finished = sorted(
            (s for s in self._sessions.values() if not s.running),
            key=lambda s: s.last_activity_at,
        )
        while len(self._sessions) >= MAX_BACKGROUND_SESSIONS and finished:
            victim = finished.pop(0)
            self._sessions.pop(victim.id, None)


_BACKGROUND_PROCESSES = BackgroundProcessManager()
atexit.register(_BACKGROUND_PROCESSES.stop_all)


def _format_session_snapshot(data: dict, output_key: str = "output_chunk") -> str:
    lines = [
        f"session_id={data['session_id']}",
        f"pid={data['pid']}",
        f"status={'running' if data['running'] else 'exited'}",
        f"returncode={'' if data['returncode'] is None else data['returncode']}",
        f"workdir={data['workdir'] or os.getcwd()}",
        f"command={data['command']}",
    ]

    if "cursor_start" in data:
        lines.append(f"cursor_start={data['cursor_start']}")
    if "next_cursor" in data:
        lines.append(f"next_cursor={data['next_cursor']}")
    if "output_truncated" in data:
        lines.append(f"output_truncated={'true' if data['output_truncated'] else 'false'}")
    if data.get("dropped_chars"):
        lines.append(f"dropped_chars={data['dropped_chars']}")
    if data.get("notice"):
        lines.append(f"notice={data['notice']}")

    lines.append("output:")
    lines.append(data.get(output_key, "") or "(no output)")
    return "\n".join(lines)


def process_list_handler(filter: str = "") -> ToolResult:
    """Lista processi attivi del sistema."""
    try:
        if sys.platform == "win32":
            cmd = "tasklist"
        else:
            cmd = "ps aux"

        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=10)
        output = r.stdout

        if filter:
            lines = output.split("\n")
            header = lines[0] if lines else ""
            matched = [l for l in lines[1:] if filter.lower() in l.lower()]
            output = header + "\n" + "\n".join(matched) if matched else f"Nessun processo con '{filter}'"

        if len(output) > 10000:
            output = output[:10000] + "\n[...troncato]"

        return ToolResult.ok(output)
    except Exception as e:
        return ToolResult.fail(str(e))


def process_sessions_handler(running_only: bool = False) -> ToolResult:
    """Lista le sessioni background gestite da openvurp."""
    try:
        rows = _BACKGROUND_PROCESSES.list_sessions(running_only=running_only)
    except Exception as e:
        return ToolResult.fail(str(e))

    if not rows:
        return ToolResult.ok("Nessuna sessione background attiva.")

    lines = []
    for row in rows:
        lines.append(
            f"{row['session_id']}  pid={row['pid']}  status={row['status']}  "
            f"elapsed={row['elapsed_s']}s  cwd={row['workdir']}\n  {row['command']}"
        )
    return ToolResult.ok("\n".join(lines))


def process_start_handler(command: str, workdir: str = "",
                          dry_run: bool = False) -> ToolResult:
    """Avvia un comando in background e ritorna un session_id."""
    if dry_run:
        cwd = os.path.abspath(workdir) if workdir else os.getcwd()
        return ToolResult.ok(
            f"DRY RUN\nwould: start background command `{command}` in `{cwd}`\n"
            "effect: no process was started"
        )
    try:
        sandbox = _build_sandbox_for_process(workdir)
        ok, reason = sandbox.check_path(sandbox.working_dir)
        if not ok:
            return ToolResult.fail(reason, error_type=ErrorType.PERMISSION)
        if sandbox.config.mode in (SandboxMode.DOCKER, SandboxMode.NSJAIL):
            return ToolResult.fail(
                "process_start supporta per ora solo sandbox mode `restricted` o `none`.",
                error_type=ErrorType.DEPENDENCY,
            )
        session = _BACKGROUND_PROCESSES.start(command=command, workdir=workdir)
    except Exception as e:
        return ToolResult.fail(str(e), error_type=ErrorType.RUNTIME)

    data = session.snapshot()
    data["output_chunk"] = ""
    data["notice"] = (
        "Sessione avviata. Usa process_read per leggere i log, "
        "process_write per inviare input, process_stop per chiuderla."
    )
    return ToolResult.ok(_format_session_snapshot(data))


def process_read_handler(session_id: str, cursor: int = -1, max_chars: int = 4000) -> ToolResult:
    """Legge output da una sessione background."""
    try:
        data = _BACKGROUND_PROCESSES.read(session_id=session_id, cursor=cursor, max_chars=max_chars)
    except KeyError as e:
        return ToolResult.fail(str(e), error_type=ErrorType.NOT_FOUND)
    except Exception as e:
        return ToolResult.fail(str(e), error_type=ErrorType.RUNTIME)

    return ToolResult.ok(_format_session_snapshot(data))


def process_write_handler(session_id: str, text: str,
                          append_newline: bool = True,
                          dry_run: bool = False) -> ToolResult:
    """Invia input stdin a una sessione background."""
    if dry_run:
        return ToolResult.ok(
            f"DRY RUN\nwould: send {len(text)} chars to session {session_id}\n"
            f"append_newline: {bool(append_newline)}\neffect: no input was sent"
        )
    try:
        session = _BACKGROUND_PROCESSES.write(
            session_id=session_id,
            text=text,
            append_newline=append_newline,
        )
    except KeyError as e:
        return ToolResult.fail(str(e), error_type=ErrorType.NOT_FOUND)
    except Exception as e:
        return ToolResult.fail(str(e), error_type=ErrorType.RUNTIME)

    data = session.snapshot()
    data["output_chunk"] = ""
    data["notice"] = "Input inviato. Usa process_read per vedere la risposta."
    return ToolResult.ok(_format_session_snapshot(data))


def process_stop_handler(session_id: str, force: bool = False,
                         wait_timeout: float = 5.0,
                         dry_run: bool = False) -> ToolResult:
    """Ferma una sessione background."""
    if dry_run:
        return ToolResult.ok(
            f"DRY RUN\nwould: stop session {session_id}\n"
            f"force: {bool(force)}\neffect: no process was stopped"
        )
    try:
        data = _BACKGROUND_PROCESSES.stop(
            session_id=session_id,
            force=force,
            wait_timeout=wait_timeout,
        )
    except KeyError as e:
        return ToolResult.fail(str(e), error_type=ErrorType.NOT_FOUND)
    except Exception as e:
        return ToolResult.fail(str(e), error_type=ErrorType.RUNTIME)

    data["output_chunk"] = ""
    if data.get("was_running"):
        data["notice"] = "Sessione terminata."
    else:
        data["notice"] = "The session was already finished."
    return ToolResult.ok(_format_session_snapshot(data))


def process_kill_handler(pid: int = 0, name: str = "",
                         dry_run: bool = False) -> ToolResult:
    """Termina un processo di sistema esterno al manager."""
    try:
        if not pid and not name:
            return ToolResult.fail("Specifica pid o name", error_type=ErrorType.VALIDATION)

        if sys.platform == "win32":
            if pid:
                cmd = f"taskkill /PID {pid} /F"
            else:
                cmd = f"taskkill /IM {name} /F"
        else:
            if pid:
                cmd = f"kill {pid}"
            else:
                cmd = f"pkill {shlex.quote(name)}"

        if dry_run:
            return ToolResult.ok(
                f"DRY RUN\nwould: execute `{cmd}`\neffect: no process was killed"
            )

        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=10)
        output = (r.stdout + r.stderr).strip()

        if r.returncode != 0:
            return ToolResult.fail(output or "Cannot terminate process", error_type=ErrorType.RUNTIME)

        return ToolResult.ok(output or "Processo terminato")
    except Exception as e:
        return ToolResult.fail(str(e))


PROCESS_LIST_TOOL = Tool(
    name="process_list",
    description="Lista processi attivi del sistema.",
    parameters={
        "type": "object",
        "properties": {
            "filter": {"type": "string", "description": "Filtro nome processo (opzionale)"},
        },
        "required": [],
    },
    handler=process_list_handler,
)

PROCESS_SESSIONS_TOOL = Tool(
    name="process_sessions",
    description="Lista le sessioni terminali background gestite da openvurp.",
    parameters={
        "type": "object",
        "properties": {
            "running_only": {"type": "boolean", "description": "Se true mostra solo le sessioni ancora attive"},
        },
        "required": [],
    },
    handler=process_sessions_handler,
)

PROCESS_START_TOOL = Tool(
    name="process_start",
    description="Avvia un comando in una sessione terminale background. Usalo per server, watch mode, dev server e job lunghi.",
    parameters={
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Comando da eseguire in background"},
            "workdir": {"type": "string", "description": "Directory di lavoro (opzionale)"},
            "dry_run": {"type": "boolean", "description": "If true, preview the start without launching a process"},
        },
        "required": ["command"],
    },
    handler=process_start_handler,
    timeout=15,
)

PROCESS_READ_TOOL = Tool(
    name="process_read",
    description="Legge i log di una sessione background usando il suo session_id.",
    parameters={
        "type": "object",
        "properties": {
            "session_id": {"type": "string", "description": "ID della sessione"},
            "cursor": {"type": "integer", "description": "Cursor ritornato da una lettura precedente; omesso = ultimi log"},
            "max_chars": {"type": "integer", "description": "Massimo caratteri da restituire (default: 4000)"},
        },
        "required": ["session_id"],
    },
    handler=process_read_handler,
    timeout=15,
)

PROCESS_WRITE_TOOL = Tool(
    name="process_write",
    description="Invia input stdin a una sessione background. Utile per shell interattive o processi che aspettano comandi.",
    parameters={
        "type": "object",
        "properties": {
            "session_id": {"type": "string", "description": "ID della sessione"},
            "text": {"type": "string", "description": "Testo da inviare allo stdin"},
            "append_newline": {"type": "boolean", "description": "Se true aggiunge newline finale"},
            "dry_run": {"type": "boolean", "description": "If true, preview the input without writing to stdin"},
        },
        "required": ["session_id", "text"],
    },
    handler=process_write_handler,
    timeout=15,
)

PROCESS_STOP_TOOL = Tool(
    name="process_stop",
    description="Ferma una sessione background avviata con process_start.",
    parameters={
        "type": "object",
        "properties": {
            "session_id": {"type": "string", "description": "ID della sessione"},
            "force": {"type": "boolean", "description": "Se true usa terminazione forzata"},
            "wait_timeout": {"type": "number", "description": "Secondi da attendere prima di forzare/ritornare"},
            "dry_run": {"type": "boolean", "description": "If true, preview the stop without terminating the process"},
        },
        "required": ["session_id"],
    },
    handler=process_stop_handler,
    timeout=15,
)

PROCESS_KILL_TOOL = Tool(
    name="process_kill",
    description="Termina un processo di sistema per PID o nome. Preferiscilo a kill/taskkill manuale quando devi gestire processi esterni.",
    parameters={
        "type": "object",
        "properties": {
            "pid": {"type": "integer", "description": "PID del processo"},
            "name": {"type": "string", "description": "Nome del processo"},
            "dry_run": {"type": "boolean", "description": "If true, preview the kill command without terminating anything"},
        },
        "required": [],
    },
    requires_approval=True,
    handler=process_kill_handler,
)
