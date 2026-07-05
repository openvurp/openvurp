"""
openvurp 4.0 — UI Layer

Display, spinner, streaming, slash command panels.
L'Agent è in core/agent.py.
"""

import os
import sys
import time
import glob
import threading
import shutil
from datetime import datetime

from core.bootstrap import resolve_workspace_file
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.syntax import Syntax
from rich import box


# ============================================
# COSTANTI
# ============================================

OPENVURP_DIR = os.path.dirname(os.path.abspath(__file__))
MEMORY_DIR = os.path.join(OPENVURP_DIR, "memory")
SKILLS_DIR = os.path.join(OPENVURP_DIR, "skills")

BRAND = "#e8654a"  # arancione openvurp (dal logo)
ACCENT = BRAND     # accento unico stile Claude Code: il nostro arancione
DIM = "bright_black"
BULLET = "⏺"
ELBOW = "⎿"

# Frasi rotanti per lo spinner, stile Claude Code
SPINNER_PHRASES = [
    "Thinking", "Pondering", "Grinding", "Connecting the dots",
    "Digging", "Reflecting", "Exploring", "Distilling",
    "Getting my bearings", "Warming up",
]
SPINNER_FRAMES = ["✢", "✳", "✶", "✻", "✽", "✻", "✶", "✳"]
GLYPH = "\u2733"  # ✳


# Slash command → short help. Drives the popup menu + autocomplete in the box.
SLASH_COMMAND_HELP = {
    "mode": "approval mode: safe | auto | plan",
    "anima": "traits the agent has grown",
    "growth": "growth report (lessons, mirror, dreams)",
    "progetti": "long-term projects",
    "fucina": "the forge: tools it built for itself",
    "sensi": "what the agent is watching",
    "fili": "bonds / follow-ups",
    "diary": "the agent's diary",
    "specchio": "mirror: corrections no longer repeated",
    "patti": "active pacts",
    "curiosita": "open questions it wants to study",
    "memory": "memory files",
    "skills": "available skills",
    "dashboard": "start web dashboard (chat in browser)",
    "update": "self-update from git, then restart",
    "restart": "restart the runtime in place",
    "doctor": "runtime diagnostics",
    "trace": "current session trace",
    "self": "agent panel",
    "integrity": "verify code integrity",
    "evolve": "self-evolution candidates",
    "voice": "toggle voice replies",
    "audio": "toggle audio / transcription",
    "mic": "microphone input",
    "setup": "guided setup wizard",
    "exit": "quit",
}

try:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.completion import Completer, Completion
    from prompt_toolkit.history import InMemoryHistory
    from prompt_toolkit.patch_stdout import patch_stdout
    from prompt_toolkit.formatted_text import ANSI, HTML
    _PT_OK = True

    class _SlashCompleter(Completer):
        """Show the command menu when you type `/` and autocomplete them."""

        def get_completions(self, document, complete_event):
            text = document.text_before_cursor
            if not text.startswith("/") or " " in text:
                return
            prefix = text[1:].lower()
            for name, desc in SLASH_COMMAND_HELP.items():
                if name.startswith(prefix):
                    yield Completion(
                        name,
                        start_position=-len(prefix),
                        display=HTML(f"/<b>{name}</b>"),
                        display_meta=desc,
                    )
except Exception:  # prompt_toolkit missing → fall back to the input() prompt
    _PT_OK = False


def load_file(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return ""


# ============================================
# UI — Rich console, spinner, streaming
# ============================================

class UI:
    def __init__(self):
        self.console = Console()
        self._spinner_active = False
        self._spinner_thread = None
        self._streaming = False
        self._stream_line_start = True
        self._model = "?"
        self._backend = "?"
        # Output prompt-safe: mentre sei fermo sul prompt (box disegnato), gli
        # output asincroni (Telegram/heartbeat) vengono messi in coda e stampati
        # appena premi Invio, così il box non viene mai rotto.
        self._at_prompt = False
        self._pending_notes: list = []
        self._notes_lock = threading.Lock()

        # prompt_toolkit: box dell'input con menu slash + autocompletamento.
        # patch_stdout gestisce l'output asincrono (heartbeat/Telegram) da solo.
        self._pt_session = None
        if _PT_OK:
            try:
                from prompt_toolkit.styles import Style
                self._pt_session = PromptSession(
                    history=InMemoryHistory(),
                    completer=_SlashCompleter(),
                    complete_while_typing=True,
                    reserve_space_for_menu=6,  # spazio per il menu / anche a schermo pieno
                    style=Style.from_dict({"bottom-toolbar": "noreverse"}),
                )
            except Exception:
                self._pt_session = None

    def notify(self, renderable) -> None:
        """Stampa un output asincrono senza rompere il box di input.

        Se l'utente è fermo sul prompt, accoda; altrimenti stampa subito.
        """
        with self._notes_lock:
            if self._at_prompt:
                self._pending_notes.append(renderable)
                return
        self.console.print(renderable)

    def flush_notes(self) -> None:
        """Svuota la coda degli output asincroni (chiamato dopo il prompt)."""
        with self._notes_lock:
            notes = self._pending_notes
            self._pending_notes = []
        for note in notes:
            self.console.print(note)

    # ── Welcome & Goodbye ──

    def welcome(self, model: str, backend: str):
        self._model = model
        self._backend = backend

        # Box di benvenuto stile Claude Code: pannello arrotondato compatto.
        # Il polpo è il marchio e resta — in formato mini (6 righe), tracciato
        # dal logo pixel-art: occhi, prompt `>_` e tentacoli.
        octopus = [
            "  ███████████",
            "  ██▄████████",
            "  ██▀█████▀██",
            "█▄██▄█████▄██▄█",
            "  ██▀ ███ ▀██",
            "▀▀ ██  █  ██ ▀▀",
        ]
        octo = Text()
        for i, line in enumerate(octopus):
            octo.append(("\n" if i else "") + line, style=BRAND)

        info = Text()
        info.append("✳ Welcome to ", style=f"bold {BRAND}")
        info.append("open", style="bold white")
        info.append("vurp", style=f"bold {BRAND}")
        info.append("!\n\n", style=f"bold {BRAND}")
        info.append("  /", style=ACCENT)
        info.append(" for commands · ", style="dim")
        info.append("/setup", style=ACCENT)
        info.append(" to reconfigure\n\n", style="dim")
        info.append(f"  model: {model} · {backend}\n", style="dim")
        info.append(f"  cwd:   {OPENVURP_DIR}", style="dim")

        grid = Table.grid(padding=(0, 2))
        grid.add_column()
        grid.add_column()
        grid.add_row(octo, info)

        self.console.print()
        self.console.print(Panel(
            grid, border_style=BRAND, box=box.ROUNDED,
            padding=(0, 1), expand=False,
        ))

    def goodbye(self):
        self.console.print(f"\n  [dim]{GLYPH} See you![/dim]\n")

    # ── Prompt ──

    def _bar(self, text: str = ""):
        cols = shutil.get_terminal_size((80, 24)).columns
        c = "\033[38;5;240m"
        r = "\033[0m"
        if text:
            pad = max(cols - len(text) - 4, 2)
            sys.stdout.write(f"{c}\u2500\u2500{text}{'\u2500' * pad}{r}\n")
        else:
            sys.stdout.write(f"{c}{'\u2500' * cols}{r}\n")
        sys.stdout.flush()

    def _status_visible(self, context_pct: int) -> str:
        ctx = f" \u00b7 ctx {context_pct}%" if context_pct > 0 else ""
        mode = getattr(self, "_approval_mode", "")
        mode_s = f" \u00b7 {mode}" if mode and mode != "safe" else ""
        return f" {self._model} \u00b7 {self._backend}{ctx}{mode_s} "

    def prompt(self, context_pct: int = 0) -> str:
        """Box input con menu slash + autocompletamento (prompt_toolkit).

        Quando digiti `/` compaiono i comandi; Tab/frecce per autocompletare.
        Box, multilinea, resize e output asincrono gestiti in modo robusto.
        """
        if self._pt_session is None:
            return self._prompt_legacy(context_pct)

        cols = shutil.get_terminal_size((80, 24)).columns
        c = "\033[38;5;240m"
        r = "\033[0m"
        status = self._status_visible(context_pct)
        pad = max(cols - len(status) - 4, 2)
        # Box stile Claude Code: bordo sopra con angoli arrotondati, riga di
        # input, bordo di chiusura dopo l'invio. Status nel bordo alto (niente
        # bottom_toolbar: ancorava la barra in basso riservando righe vuote).
        # Il menu `/` compare solo quando digiti.
        top = f"{c}\u256d\u2500{status}{'\u2500' * pad}\u256e{r}"
        bottom = f"{c}\u2570{'\u2500' * max(cols - 2, 2)}\u256f{r}"

        sys.stdout.write("\n" + top + "\n")
        sys.stdout.flush()
        try:
            with patch_stdout(raw=True):
                result = self._pt_session.prompt(
                    ANSI(f"{c}\u2502{r} \033[1;97m>\033[0m "),
                    reserve_space_for_menu=6,
                )
        except EOFError:
            result = "/exit"
        except KeyboardInterrupt:
            result = ""
        sys.stdout.write(bottom + "\n")
        sys.stdout.flush()
        return result or ""

    def _prompt_legacy(self, context_pct: int = 0) -> str:
        """Fallback senza prompt_toolkit: box semplice + input()."""
        cols = shutil.get_terminal_size((80, 24)).columns
        c = "\033[38;5;240m"
        r = "\033[0m"
        status = self._status_visible(context_pct)
        pad = max(cols - len(status) - 4, 2)
        sys.stdout.write(f"\n{c}\u256d\u2500{status}{'\u2500' * pad}\u256e{r}\n")
        sys.stdout.flush()
        self._at_prompt = True
        try:
            result = input(f"{c}\u2502{r} \033[1;97m>\033[0m ")
        except EOFError:
            result = "/exit"
        finally:
            self._at_prompt = False
        sys.stdout.write(f"{c}\u2570{'\u2500' * max(cols - 2, 2)}\u256f{r}\n")
        sys.stdout.flush()
        self.flush_notes()
        return result

    def confirm(self, question: str) -> bool:
        self.console.print(f"\n  [yellow]! {question}[/yellow]")
        r = input("  \033[1m(y/n): \033[0m").strip().lower()
        return r in ('s', 'si', 'y', 'yes', '')

    def confirm_choice(self, question: str) -> str:
        """Conferma a tre vie: sì / no / sempre (ricorda per 8h)."""
        self.console.print(f"\n  [yellow]! {question}[/yellow]")
        r = input("  \033[1m(y/n/always): \033[0m").strip().lower()
        if r in ('sempre', 'always', 'a'):
            return "always"
        if r in ('s', 'si', 'sì', 'y', 'yes', ''):
            return "yes"
        return "no"

    # ── Spinner ──

    def start_spinner(self, text: str = "Thinking..."):
        self.stop_spinner()
        self._spinner_active = True

        # Frase rotante stile Claude Code quando il chiamante non specifica
        import random
        if text in ("Thinking...", ""):
            phrase = random.choice(SPINNER_PHRASES)
        else:
            phrase = text.rstrip(".")
        started = time.time()

        def spin():
            i = 0
            while self._spinner_active:
                f = SPINNER_FRAMES[i % len(SPINNER_FRAMES)]
                elapsed = int(time.time() - started)
                line = (f"\r\033[38;5;208m{f}\033[0m "
                        f"\033[38;5;245m{phrase}\u2026 "
                        f"({elapsed}s \u00b7 ctrl+c per interrompere)\033[0m   ")
                sys.stdout.write(line)
                sys.stdout.flush()
                i += 1
                time.sleep(0.12)

        self._spinner_thread = threading.Thread(target=spin, daemon=True)
        self._spinner_thread.start()

    def stop_spinner(self):
        if not self._spinner_active:
            return
        self._spinner_active = False
        if self._spinner_thread:
            self._spinner_thread.join(timeout=0.5)
            self._spinner_thread = None
        sys.stdout.write(f"\r{' ' * 100}\r")
        sys.stdout.flush()

    # ── Response display ──

    def start_response(self):
        # Stile Claude Code: bullet + testo sulla stessa riga
        sys.stdout.write(f"\n\033[97m{BULLET}\033[0m ")
        sys.stdout.flush()
        self._streaming = True
        self._stream_line_start = False

    def stream_token(self, text: str):
        """Scrive un delta di testo in streaming reale (nessun delay artificiale)."""
        out = []
        for ch in text:
            if ch == '\n':
                out.append("\n")
                self._stream_line_start = True
            else:
                if self._stream_line_start:
                    out.append("  ")
                    self._stream_line_start = False
                out.append(ch)
        sys.stdout.write("".join(out))
        sys.stdout.flush()

    def stream_text(self, text: str):
        """Mostra testo completo (stesso formato dello streaming, senza typing finto)."""
        self.stream_token(text)

    def end_response(self):
        if self._streaming:
            sys.stdout.write("\n")
            sys.stdout.flush()
            self._streaming = False

    def openvurp_say(self, text: str):
        """Quick message without typing effect."""
        lines = text.split('\n')
        first = True
        self.console.print()
        for line in lines:
            if first:
                self.console.print(f"[white]{BULLET}[/white] {line}")
                first = False
            elif line.strip():
                self.console.print(f"  {line}")
            else:
                self.console.print()
        self.console.print()

    # ── Tool display ──

    def show_cmd(self, cmd: str):
        """Stile Claude Code: ⏺ Bash(comando)"""
        clean = " ".join(cmd.strip().split())
        if len(clean) > 120:
            clean = clean[:117] + "..."
        self.console.print(f"\n[green]{BULLET}[/green] [bold]Bash[/bold]([{DIM}]{clean}[/{DIM}])")

    def show_tool(self, tool_name: str, tool_args: dict | None = None):
        """Stile Claude Code: ⏺ ToolName(arg principale)"""
        label = "".join(p.capitalize() for p in tool_name.replace("-", "_").split("_") if p) or "Tool"
        preview = ""
        if isinstance(tool_args, dict) and tool_args:
            for key in ("path", "file", "query", "url", "name", "command", "pattern", "content", "task"):
                if tool_args.get(key):
                    preview = str(tool_args[key])
                    break
            else:
                preview = str(next(iter(tool_args.values())))
        preview = " ".join(preview.split())
        if len(preview) > 100:
            preview = preview[:97] + "..."
        self.console.print(f"\n[green]{BULLET}[/green] [bold]{label}[/bold]([{DIM}]{preview}[/{DIM}])")

    def show_output(self, output: str, is_error: bool = False, max_lines: int = 5):
        if not output or output == "(no output)":
            self.console.print(f"  [{DIM}]{ELBOW}  (no output)[/{DIM}]")
            return
        lines = [l for l in output.strip().split('\n')]
        if is_error:
            shown = [l for l in lines[-min(max_lines, len(lines)):] if l.strip()]
            for i, line in enumerate(shown):
                glyph = ELBOW if i == 0 else " "
                self.console.print(f"  [red]{glyph}  {line.strip()[:140]}[/red]")
        else:
            for i, line in enumerate(lines[:max_lines]):
                glyph = ELBOW if i == 0 else " "
                self.console.print(f"  [{DIM}]{glyph}  {line[:140]}[/{DIM}]")
            if len(lines) > max_lines:
                self.console.print(f"  [{DIM}]   … +{len(lines) - max_lines} righe[/{DIM}]")

    def status(self, text: str):
        clean = text.strip()
        if clean.startswith("[") and clean.endswith("]"):
            clean = clean[1:-1]
        self.console.print(f"  [{DIM}]{ELBOW}  {clean}[/{DIM}]")

    def error(self, text: str):
        self.console.print(f"\n  [red]x {text}[/red]")

    # ── Slash command panels ──

    def show_memory_table(self):
        os.makedirs(MEMORY_DIR, exist_ok=True)
        files = []
        for f in sorted(os.listdir(MEMORY_DIR)):
            fp = os.path.join(MEMORY_DIR, f)
            if os.path.isfile(fp):
                stat = os.stat(fp)
                preview = ""
                try:
                    with open(fp, "r", encoding="utf-8") as fh:
                        preview = fh.read(100).replace('\n', ' ')
                except Exception:
                    pass
                files.append({
                    "filename": f,
                    "size": stat.st_size,
                    "modified": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
                    "preview": preview,
                })

        if not files:
            self.console.print(f"\n  [{DIM}](empty memory)[/{DIM}]\n")
            return

        table = Table(box=box.SIMPLE_HEAVY, border_style=DIM, padding=(0, 1), show_edge=False)
        table.add_column("File", style="white")
        table.add_column("Size", style="dim", justify="right")
        table.add_column("Modified", style="dim")
        table.add_column("Preview", style=DIM, max_width=40, no_wrap=True)

        for f in files:
            table.add_row(f["filename"], f"{f['size']} B", f["modified"], f["preview"][:40])

        # Subdirectories
        subdirs = []
        for d in ['lessons', 'projects', 'sessions']:
            dp = os.path.join(MEMORY_DIR, d)
            if os.path.exists(dp):
                count = len([x for x in os.listdir(dp) if os.path.isfile(os.path.join(dp, x))])
                subdirs.append(f"{d}/: {count} file")

        self.console.print()
        self.console.print(f"  [bold {ACCENT}]{GLYPH} Memory[/bold {ACCENT}]")
        self.console.print(table)
        if subdirs:
            for s in subdirs:
                self.console.print(f"    [{DIM}]{s}[/{DIM}]")
        self.console.print()

    def show_skills_table(self):
        skills = []
        if os.path.exists(SKILLS_DIR):
            for f in sorted(glob.glob(os.path.join(SKILLS_DIR, "*.md"))):
                name = os.path.splitext(os.path.basename(f))[0]
                desc = ""
                try:
                    with open(f, "r", encoding="utf-8") as fh:
                        first = fh.readline().strip()
                        if first.startswith("#"):
                            first = fh.readline().strip()
                        desc = first[:80]
                except Exception:
                    pass
                skills.append({"name": name, "description": desc})

        if not skills:
            self.console.print(f"\n  [{DIM}](no skills -- add .md files in skills/)[/{DIM}]\n")
            return

        table = Table(box=box.SIMPLE_HEAVY, border_style=DIM, padding=(0, 1), show_edge=False)
        table.add_column("Name", style="white bold")
        table.add_column("Description", style="dim")

        for s in skills:
            table.add_row(s["name"], s["description"])

        self.console.print()
        self.console.print(f"  [bold {ACCENT}]{GLYPH} Skills[/bold {ACCENT}]")
        self.console.print(table)
        self.console.print(f"    [{DIM}]Add .md files in skills/[/{DIM}]")
        self.console.print()

    def show_self_panel(self):
        content = Text()
        # Core modules
        for f in ['SOUL.md', 'config.py', 'agent.py', 'main.py']:
            if f.endswith('.md'):
                _, path = resolve_workspace_file(OPENVURP_DIR, f)
            else:
                path = os.path.join(OPENVURP_DIR, f)
            if os.path.exists(path):
                size = os.path.getsize(path)
                lines = load_file(path).count('\n')
                content.append(f"  {os.path.basename(path)}: ", style="dim")
                content.append(f"{lines} righe, {size} bytes\n", style="white")

        # Core modules
        core_dir = os.path.join(OPENVURP_DIR, "core")
        if os.path.exists(core_dir):
            content.append(f"\n  core/\n", style=f"bold {ACCENT}")
            for f in sorted(os.listdir(core_dir)):
                if f.endswith('.py') and f != '__init__.py':
                    path = os.path.join(core_dir, f)
                    size = os.path.getsize(path)
                    lines = load_file(path).count('\n')
                    content.append(f"    {f}: ", style="dim")
                    content.append(f"{lines} righe, {size} bytes\n", style="white")

        # Tools
        tools_dir = os.path.join(OPENVURP_DIR, "tools")
        if os.path.exists(tools_dir):
            content.append(f"\n  tools/\n", style=f"bold {ACCENT}")
            for f in sorted(os.listdir(tools_dir)):
                if f.endswith('.py') and f != '__init__.py':
                    path = os.path.join(tools_dir, f)
                    size = os.path.getsize(path)
                    lines = load_file(path).count('\n')
                    content.append(f"    {f}: ", style="dim")
                    content.append(f"{lines} righe, {size} bytes\n", style="white")

        content.append(f"\n  dir: ", style="dim")
        content.append(f"{OPENVURP_DIR}\n", style="white")

        self.console.print()
        self.console.print(Panel(
            content,
            title=f"[bold]{GLYPH} Il mio codice[/bold]", title_align="left",
            border_style=ACCENT, box=box.ROUNDED, padding=(0, 1),
        ))
        self.console.print()

    def show_trace(self, trace_text: str):
        """Mostra trace della sessione corrente."""
        self.console.print()
        self.console.print(Panel(
            Text(trace_text, style="white"),
            title=f"[bold]{GLYPH} Session Trace[/bold]", title_align="left",
            border_style=ACCENT, box=box.ROUNDED, padding=(0, 1),
        ))
        self.console.print()

    def show_growth(self, report_text: str):
        """Diario di crescita: quanto l'agente è cresciuto con l'owner."""
        self.console.print()
        self.console.print(Panel(
            Text(report_text, style="white"),
            title=f"[bold]{GLYPH} Growth[/bold]", title_align="left",
            border_style=ACCENT, box=box.ROUNDED, padding=(0, 1),
        ))
        self.console.print()

    def show_doctor(self, report_text: str):
        self.console.print()
        self.console.print(Panel(
            Text(report_text, style="white"),
            title=f"[bold]{GLYPH} Doctor[/bold]", title_align="left",
            border_style=ACCENT, box=box.ROUNDED, padding=(0, 1),
        ))
        self.console.print()

    def show_evolve(self):
        ep = os.path.join(MEMORY_DIR, "evoluzione.md")
        text = load_file(ep) if os.path.exists(ep) else ""
        if not text:
            self.console.print(f"\n  [{DIM}](no evolution yet)[/{DIM}]\n")
            return
        self.console.print()
        self.console.print(Panel(
            Text(text[:1000], style="dim"),
            title=f"[bold]{GLYPH} Evolution[/bold]", title_align="left",
            border_style=ACCENT, box=box.ROUNDED, padding=(0, 1),
        ))
        self.console.print()
