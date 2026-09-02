"""
openvurp 4.0 — UI Layer

Display, spinner, streaming, slash command panels.
L'Agent è in core/agent.py.
"""

import os
import sys
import time
import threading
from datetime import datetime

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.live import Live
from rich.markdown import Markdown
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
        self._response_buffer = ""
        self._response_live = None
        self._model = "?"
        self._backend = "?"
        # Output prompt-safe: mentre sei fermo sul prompt (box disegnato), gli
        # output asincroni (heartbeat/notifiche) vengono messi in coda e stampati
        # appena premi Invio, così il box non viene mai rotto.
        self._at_prompt = False
        self._pending_notes: list = []
        self._notes_lock = threading.Lock()

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

    def welcome(self, model: str, backend: str, hint: str = ""):
        self._model = model
        self._backend = backend

        # Box di benvenuto stile Claude Code: pannello arrotondato compatto.
        # Il polpo è IL LOGO — non si modifica: pixel-art originale dal marchio.
        octopus = [
            "   ███████████",
            "   ██▄████████",
            "   ██▀█████▀██",
            "▄▄ ██▄█████▄██ ▄▄",
            "██▄███████████▄██",
            "    ██▀███▀██",
            "██▄███ ███ ███▄██",
            "▀▀▀▀▀  ▀▀▀  ▀▀▀▀▀",
        ]
        octo = Text()
        for i, line in enumerate(octopus):
            octo.append(("\n" if i else "") + line, style=BRAND)

        info = Text()
        info.append("\n")  # centra le info rispetto al polpo
        info.append("✳ Welcome to ", style=f"bold {BRAND}")
        info.append("open", style="bold white")
        info.append("vurp", style=f"bold {BRAND}")
        info.append("!\n\n", style=f"bold {BRAND}")
        if hint:
            info.append("  " + hint + "\n\n", style="dim")
        else:
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

    def _response_renderable(self, text: str):
        """Risposta Markdown con il bullet openvurp allineato al contenuto."""
        grid = Table.grid(padding=(0, 1), expand=True)
        grid.add_column(width=1, no_wrap=True)
        grid.add_column(ratio=1)
        grid.add_row(Text(BULLET, style="white"), Markdown(text or " "))
        return grid

    def start_response(self):
        # Rich Live ridisegna il Markdown parziale a ogni delta: lo streaming
        # resta reale ma marker come **, ``` e # non rimangono grezzi.
        if self._response_live is not None:
            try:
                self._response_live.stop()
            except Exception:
                pass
        self._response_buffer = ""
        self._streaming = True
        self._response_live = Live(
            self._response_renderable(""),
            console=self.console,
            refresh_per_second=20,
            transient=True,
            auto_refresh=False,
        )
        self._response_live.start(refresh=True)

    def stream_token(self, text: str):
        """Aggiorna il Markdown con un delta reale, senza typing artificiale."""
        if not text:
            return
        self._response_buffer += str(text)
        if self._response_live is not None:
            self._response_live.update(
                self._response_renderable(self._response_buffer), refresh=True,
            )

    def stream_text(self, text: str):
        """Mostra testo completo usando lo stesso renderer Markdown live."""
        self.stream_token(text)

    def end_response(self):
        if self._streaming:
            live = self._response_live
            self._response_live = None
            if live is not None:
                live.stop()
            self.console.print(self._response_renderable(self._response_buffer))
            self._streaming = False

    def openvurp_say(self, text: str):
        """Messaggio completo, renderizzato con lo stesso Markdown della chat."""
        self.console.print(self._response_renderable(str(text or "")))

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

