"""
openvurp Tool — Search

Grep, glob, find con regex e filetype filter.
"""

from __future__ import annotations

import fnmatch
import os
import re
import time

from core.tools import Tool, ToolResult, ErrorType


# Directory che non contengono mai codice del progetto e che su un mount lento
# (es. /mnt/c di WSL2) costano piu' di tutto il resto messo insieme.
PRUNE_DIRS = {
    "node_modules", "__pycache__", ".git", "venv", ".venv", "env", "envs",
    "site-packages", "dist-packages", "dist", "build", "target", "vendor",
    ".tox", ".nox", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".cache",
    ".next", ".nuxt", ".gradle", ".idea", ".vscode", "coverage", "htmlcov",
    "downloads", "eggs", ".eggs", "bower_components", ".terraform",
}


def _search_limits() -> tuple[float, int]:
    """Tetto di tempo e di file per una singola ricerca."""
    try:
        import config as cfg
        seconds = float(getattr(cfg, "SEARCH_TIME_BUDGET_SECONDS", 20) or 20)
        max_files = int(getattr(cfg, "SEARCH_MAX_FILES_SCANNED", 20000) or 20000)
    except Exception:
        seconds, max_files = 20.0, 20000
    return max(1.0, min(seconds, 300.0)), max(500, min(max_files, 500000))


# Estensioni che non si leggono riga per riga: aprirle e' solo I/O sprecato,
# e su un mount lento e' proprio quello che esaurisce il budget di ricerca.
BINARY_SUFFIXES = (
    ".pyc", ".pyo", ".so", ".dll", ".dylib", ".exe", ".bin", ".o", ".a",
    ".zip", ".gz", ".bz2", ".xz", ".7z", ".tar", ".rar", ".jar", ".whl",
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".svgz",
    ".pdf", ".mp3", ".mp4", ".wav", ".ogg", ".avi", ".mov", ".webm",
    ".db", ".sqlite", ".sqlite3", ".pack", ".idx", ".woff", ".woff2", ".ttf",
)
GREP_MAX_FILE_BYTES = 2_000_000


def _walk_bounded(path: str, deadline: float, max_files: int):
    """`os.walk` con potatura aggressiva, tetto di tempo e tetto di file.

    Restituisce ``(root, files, truncated_reason)``: una ricerca che si ferma
    deve dirlo, altrimenti "nessun risultato" viene letto come "non esiste".

    Un virtualenv si riconosce dal suo marcatore ``pyvenv.cfg``, non dal nome:
    l'utente puo' chiamarlo `envi` o `pyenv` e la lista dei nomi non basta. Il
    marcatore si legge pero' dai file che ``os.walk`` ha gia' elencato, senza
    uno ``stat`` per directory — su un mount 9p quello costerebbe piu' della
    ricerca stessa.
    """
    from collections import deque

    scanned = 0
    queue = deque([path])
    while queue:
        # Ampiezza, non profondita': se il budget finisce, deve essere finito
        # sui livelli alti (dove sta il codice) e non dentro un ramo profondo
        # di dati. `os.walk` in profondita' faceva dire "nessun match" a una
        # grep il cui match era in una directory di primo livello mai visitata.
        root = queue.popleft()
        try:
            with os.scandir(root) as entries:
                dirs, files = [], []
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            dirs.append(entry.name)
                        else:
                            files.append(entry.name)
                    except OSError:
                        continue
        except (PermissionError, OSError):
            continue

        if "pyvenv.cfg" in files:
            continue              # radice di un virtualenv: non scendere

        for name in dirs:
            if (name.startswith(".") or name in PRUNE_DIRS
                    or name.endswith(".egg-info")):
                continue
            queue.append(os.path.join(root, name))

        scanned += len(files)
        if time.monotonic() > deadline:
            yield root, files, "tempo"
            return
        if scanned > max_files:
            yield root, files, "numero di file"
            return
        yield root, files, ""


def grep_handler(pattern: str, path: str = ".", file_pattern: str = "",
                 max_results: int = 50, ignore_case: bool = False) -> ToolResult:
    """Cerca un pattern nei file."""
    try:
        flags = re.IGNORECASE if ignore_case else 0
        try:
            regex = re.compile(pattern, flags)
        except re.error as e:
            return ToolResult.fail(f"Regex non valida: {e}", error_type=ErrorType.VALIDATION)

        results = []
        searched = 0
        budget, max_files = _search_limits()
        deadline = time.monotonic() + budget
        truncated = ""

        for root, files, stop in _walk_bounded(path, deadline, max_files):
            for fname in files:
                if file_pattern and not fnmatch.fnmatch(fname, file_pattern):
                    continue
                # Leggere i file e' la parte cara: il tempo va controllato qui,
                # non solo fra una directory e l'altra.
                if time.monotonic() > deadline:
                    truncated = "tempo"
                    break

                if fname.lower().endswith(BINARY_SUFFIXES):
                    continue

                fpath = os.path.join(root, fname)
                try:
                    if os.path.getsize(fpath) > GREP_MAX_FILE_BYTES:
                        continue
                    with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
                        for i, line in enumerate(f, 1):
                            if regex.search(line):
                                rel = os.path.relpath(fpath, path)
                                results.append(f"{rel}:{i}: {line.rstrip()[:200]}")
                                if len(results) >= max_results:
                                    break
                    searched += 1
                except (PermissionError, IsADirectoryError, OSError):
                    continue

                if len(results) >= max_results:
                    break
            if stop:
                truncated = stop
            if len(results) >= max_results or truncated:
                break

        note = _truncation_note(truncated)
        if not results:
            return ToolResult.ok(
                f"No match for '{pattern}' in {path} ({searched} files searched)" + note
            )

        header = f"[{len(results)} match in {searched} file]\n"
        return ToolResult.ok(header + "\n".join(results) + note)

    except Exception as e:
        return ToolResult.fail(str(e))


def _truncation_note(reason: str) -> str:
    """Dice al modello che la ricerca si e' fermata, e come restringerla."""
    if not reason:
        return ""
    return (
        f"\n\n[RICERCA TRONCATA: raggiunto il limite di {reason}. "
        f"Questi risultati sono PARZIALI: non concludere che il resto non "
        f"esista. Restringi il campo con `path` (es. 'core') o con un pattern "
        f"piu' specifico.]"
    )


def _expand_braces(pattern: str, limit: int = 64) -> list[str]:
    """Espande gruppi glob semplici come ``{A.md,B.md}``.

    ``fnmatch`` non implementa il brace-glob delle shell. Codex e altri LLM lo
    usano spesso per chiedere piu' nomi in una sola chiamata, quindi lo
    normalizziamo qui senza passare da una shell.
    """
    expanded = [str(pattern or "")]
    while len(expanded) < limit:
        changed = False
        next_patterns: list[str] = []
        for current in expanded:
            match = re.search(r"\{([^{}]*,[^{}]*)\}", current)
            if not match:
                next_patterns.append(current)
                continue
            changed = True
            prefix, suffix = current[:match.start()], current[match.end():]
            choices = [part.strip() for part in match.group(1).split(",") if part.strip()]
            next_patterns.extend(prefix + choice + suffix for choice in choices)
            if len(next_patterns) >= limit:
                break
        expanded = next_patterns[:limit]
        if not changed:
            break
    return expanded or [str(pattern or "")]


def glob_handler(pattern: str, path: str = ".") -> ToolResult:
    """Cerca file per pattern glob."""
    try:
        patterns = _expand_braces(pattern)
        results = []
        seen: set[str] = set()
        budget, max_files = _search_limits()
        deadline = time.monotonic() + budget
        truncated = ""

        for root, files, stop in _walk_bounded(path, deadline, max_files):
            for fname in files:
                full_path = os.path.join(root, fname)
                fpath = os.path.relpath(full_path, path)
                normalized = fpath.replace(os.sep, "/")
                if any(
                    fnmatch.fnmatch(fname, candidate)
                    or fnmatch.fnmatch(normalized, candidate.replace(os.sep, "/"))
                    for candidate in patterns
                ) and fpath not in seen:
                    seen.add(fpath)
                    try:
                        size = os.path.getsize(full_path)
                    except OSError:
                        size = 0
                    results.append(f"{fpath}  ({size} B)")
                    if len(results) >= 200:
                        break

            if stop:
                truncated = stop
            if len(results) >= 200 or truncated:
                break

        note = _truncation_note(truncated)
        if not results:
            return ToolResult.ok(f"No file matches '{pattern}' in {path}" + note)

        return ToolResult.ok(
            f"[{len(results)} file]\n" + "\n".join(sorted(results)) + note
        )

    except Exception as e:
        return ToolResult.fail(str(e))


GREP_TOOL = Tool(
    name="grep",
    description="Cerca un pattern (regex) nei file del workspace. Preferiscilo a shell grep/rg quando ti basta cercare nel codice.",
    parameters={
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Pattern regex da cercare"},
            "path": {"type": "string", "description": "Directory dove cercare (default: .)"},
            "file_pattern": {"type": "string", "description": "Filtro filename (es: '*.py')"},
            "max_results": {"type": "integer", "description": "Max risultati (default: 50)"},
            "ignore_case": {"type": "boolean", "description": "Case insensitive (default: false)"}
        },
        "required": ["pattern"]
    },
    handler=grep_handler
)

GLOB_TOOL = Tool(
    name="find_files",
    description=(
        "Cerca file per nome o pattern glob nel workspace. Supporta anche "
        "gruppi come '{AGENTS.md,SOUL.md,USER.md}' e percorsi come 'core/*.py'. "
        "Preferiscilo a shell find/rg --files per discovery file."
    ),
    parameters={
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Pattern glob (es: '*.py', 'test_*')"},
            "path": {"type": "string", "description": "Directory dove cercare (default: .)"}
        },
        "required": ["pattern"]
    },
    handler=glob_handler
)
