"""
openvurp Tool — File Operations

Read, Write, Edit con backup, diff, encoding detection.
"""

from __future__ import annotations

import os
import shutil
from datetime import datetime

from core.tools import Tool, ToolResult, ErrorType, RetryPolicy


def _fix_path(path: str) -> str:
    """Converte path Windows in path WSL se necessario.

    C:\\Users\\alice\\file.txt → /mnt/c/Users/alice/file.txt
    C:/Users/alice/file.txt   → /mnt/c/Users/alice/file.txt
    /mnt/c/... → invariato
    percorso/relativo → invariato
    """
    if not path:
        return path

    path = path.strip().strip('"').strip("'")

    # Già un path Unix/WSL
    if path.startswith("/"):
        return path

    # Path Windows: C:\... o C:/...
    if len(path) >= 3 and path[1] == ":" and path[2] in ("/", "\\"):
        drive = path[0].lower()
        rest = path[3:].replace("\\", "/")
        return f"/mnt/{drive}/{rest}"

    # Path relativo — risolvi rispetto alla directory openvurp
    openvurp_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(openvurp_dir, path.replace("\\", "/"))


def _check_sandbox(path: str):
    """Confina i file tool al workspace quando il sandbox è attivo.

    In SANDBOX_MODE != 'off' consente solo percorsi dentro il workspace (OPENVURP_DIR)
    o in SANDBOX_ALLOWED_PATHS; altrimenti blocca (anti path-traversal / lettura
    di file di sistema come /etc/passwd, ~/.ssh, ecc.). Ritorna un ToolResult di
    blocco se il path è fuori, altrimenti None.
    """
    try:
        import config
        mode = str(getattr(config, "SANDBOX_MODE", "restricted") or "restricted")
        if mode == "off":
            return None
        bases = [str(getattr(config, "OPENVURP_DIR", "")) or
                 os.path.dirname(os.path.dirname(os.path.abspath(__file__)))]
        bases += [str(p) for p in (getattr(config, "SANDBOX_ALLOWED_PATHS", []) or [])]
    except Exception:
        return None
    try:
        real = os.path.realpath(path)
    except Exception:
        real = path
    for base in bases:
        if not base:
            continue
        try:
            base_real = os.path.realpath(base)
        except Exception:
            continue
        if real == base_real or real.startswith(base_real + os.sep):
            return None
    return ToolResult.fail(
        f"Path outside the workspace, blocked by the sandbox: {path}. "
        f"Allow extra paths with SANDBOX_ALLOWED_PATHS, or set SANDBOX_MODE=off.",
        error_type=ErrorType.PERMISSION,
    )


def read_file_handler(path: str, start_line: int = 0, end_line: int = 0) -> ToolResult:
    """Legge un file con opzionale range di righe."""
    path = _fix_path(path)
    _sb = _check_sandbox(path)
    if _sb is not None:
        return _sb
    try:
        if not os.path.exists(path):
            return ToolResult.fail(
                f"File non trovato: {path}",
                error_type=ErrorType.NOT_FOUND
            )
        if not os.path.isfile(path):
            return ToolResult.fail(f"Non è un file: {path}")

        # Detect encoding
        encoding = "utf-8"
        try:
            with open(path, "r", encoding=encoding) as f:
                content = f.read()
        except UnicodeDecodeError:
            encoding = "latin-1"
            with open(path, "r", encoding=encoding) as f:
                content = f.read()

        lines = content.split("\n")

        if start_line or end_line:
            start = max(0, start_line - 1)  # 1-indexed
            end = end_line if end_line else len(lines)
            selected = lines[start:end]
            # Mostra con numeri di riga
            numbered = []
            for i, line in enumerate(selected, start=start + 1):
                numbered.append(f"{i:4d} | {line}")
            output = "\n".join(numbered)
        else:
            if len(content) > 30000:
                content = content[:15000] + "\n[...TRONCATO...]\n" + content[-5000:]
            output = content

        info = f"[{len(lines)} righe, {os.path.getsize(path)} bytes, {encoding}]"
        return ToolResult.ok(f"{info}\n{output}")

    except PermissionError:
        return ToolResult.fail(
            f"Permesso negato: {path}",
            error_type=ErrorType.PERMISSION
        )
    except Exception as e:
        return ToolResult.fail(str(e))


def write_file_handler(path: str, content: str, backup: bool = True,
                       dry_run: bool = False) -> ToolResult:
    """Scrive un file con backup automatico."""
    path = _fix_path(path)
    _sb = _check_sandbox(path)
    if _sb is not None:
        return _sb
    if dry_run:
        lines = content.count("\n") + 1
        return ToolResult.ok(
            f"DRY RUN\nwould: write {lines} righe / {len(content)} bytes to {path}\n"
            f"backup: {bool(backup)}\neffect: no file was changed"
        )
    try:
        # Backup se il file esiste
        if backup and os.path.exists(path):
            backup_dir = os.path.join(os.path.dirname(path) or ".", ".openvurp_backups")
            os.makedirs(backup_dir, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_path = os.path.join(backup_dir, f"{os.path.basename(path)}.{ts}.bak")
            shutil.copy2(path, backup_path)

        # Crea directory se necessario
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)

        with open(path, "w", encoding="utf-8") as f:
            f.write(content)

        size = os.path.getsize(path)
        lines = content.count("\n") + 1
        return ToolResult.ok(f"Scritto: {path} ({lines} righe, {size} bytes)")

    except PermissionError:
        return ToolResult.fail(
            f"Permesso negato: {path}",
            error_type=ErrorType.PERMISSION
        )
    except Exception as e:
        return ToolResult.fail(str(e))


def edit_file_handler(path: str, old_string: str, new_string: str,
                      replace_all: bool = False,
                      dry_run: bool = False) -> ToolResult:
    """Edit con sostituzione old_string → new_string."""
    path = _fix_path(path)
    _sb = _check_sandbox(path)
    if _sb is not None:
        return _sb
    try:
        if not os.path.exists(path):
            return ToolResult.fail(
                f"File non trovato: {path}",
                error_type=ErrorType.NOT_FOUND
            )

        with open(path, "r", encoding="utf-8") as f:
            content = f.read()

        if old_string not in content:
            return ToolResult.fail(
                f"Stringa non trovata nel file: {old_string[:80]}..."
            )

        count = content.count(old_string)
        if count > 1 and not replace_all:
            return ToolResult.fail(
                f"Stringa trovata {count} volte. Usa replace_all=true o fornisci più contesto."
            )

        if dry_run:
            replaced = count if replace_all else 1
            return ToolResult.ok(
                f"DRY RUN\nwould: replace {replaced} occurrence(s) in {path}\n"
                f"old_len: {len(old_string)}\nnew_len: {len(new_string)}\n"
                "effect: no file was changed"
            )

        # Backup
        backup_dir = os.path.join(os.path.dirname(path) or ".", ".openvurp_backups")
        os.makedirs(backup_dir, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = os.path.join(backup_dir, f"{os.path.basename(path)}.{ts}.bak")
        shutil.copy2(path, backup_path)

        if replace_all:
            new_content = content.replace(old_string, new_string)
            replaced = count
        else:
            new_content = content.replace(old_string, new_string, 1)
            replaced = 1

        with open(path, "w", encoding="utf-8") as f:
            f.write(new_content)

        return ToolResult.ok(f"Modificato: {path} ({replaced} sostituzion{'i' if replaced > 1 else 'e'})")

    except Exception as e:
        return ToolResult.fail(str(e))


def edit_lines_handler(path: str, line: int, content: str,
                       end_line: int = 0, insert: bool = False,
                       dry_run: bool = False) -> ToolResult:
    """Modifica righe per numero. Più semplice di edit_file per patch mirate.

    - line=5, content="nuovo" → sostituisce riga 5
    - line=5, end_line=8, content="..." → sostituisce righe 5-8
    - line=5, insert=true, content="..." → inserisce PRIMA della riga 5
    """
    path = _fix_path(path)
    _sb = _check_sandbox(path)
    if _sb is not None:
        return _sb
    try:
        if not os.path.exists(path):
            return ToolResult.fail(f"File non trovato: {path}", error_type=ErrorType.NOT_FOUND)

        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()

        total = len(lines)
        if line < 1 or line > total + 1:
            return ToolResult.fail(f"Riga {line} fuori range (file ha {total} righe)")

        if dry_run:
            end = end_line if end_line else line
            mode = "insert before" if insert else "replace"
            return ToolResult.ok(
                f"DRY RUN\nwould: {mode} lines {line}-{end} in {path}\n"
                f"content_bytes: {len(content)}\neffect: no file was changed"
            )

        # Backup
        backup_dir = os.path.join(os.path.dirname(path) or ".", ".openvurp_backups")
        os.makedirs(backup_dir, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        shutil.copy2(path, os.path.join(backup_dir, f"{os.path.basename(path)}.{ts}.bak"))

        # Assicura newline finale
        new_lines = content.split("\n")
        new_lines = [l + "\n" if not l.endswith("\n") else l for l in new_lines]

        idx = line - 1  # 0-indexed

        if insert:
            # Inserisci prima della riga
            lines[idx:idx] = new_lines
            action = f"Inserite {len(new_lines)} righe prima della riga {line}"
        else:
            # Sostituisci righe
            end = end_line if end_line else line
            if end < line:
                end = line
            if end > total:
                end = total
            lines[idx:end] = new_lines
            replaced = end - idx
            action = f"Sostituite righe {line}-{end} ({replaced} → {len(new_lines)})"

        with open(path, "w", encoding="utf-8") as f:
            f.writelines(lines)

        return ToolResult.ok(f"{action} in {path}")

    except Exception as e:
        return ToolResult.fail(str(e))


def append_file_handler(path: str, content: str, dry_run: bool = False) -> ToolResult:
    """Aggiunge contenuto alla fine di un file."""
    path = _fix_path(path)
    _sb = _check_sandbox(path)
    if _sb is not None:
        return _sb
    if dry_run:
        return ToolResult.ok(
            f"DRY RUN\nwould: append {len(content)} bytes to {path}\n"
            "effect: no file was changed"
        )
    try:
        if not content:
            return ToolResult.fail("No content to add")

        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

        with open(path, "a", encoding="utf-8") as f:
            if not content.startswith("\n"):
                # Assicura che ci sia un newline prima
                try:
                    with open(path, "r", encoding="utf-8") as rf:
                        existing = rf.read()
                    if existing and not existing.endswith("\n"):
                        f.write("\n")
                except FileNotFoundError:
                    pass
            f.write(content)
            if not content.endswith("\n"):
                f.write("\n")

        return ToolResult.ok(f"Aggiunto a: {path}")

    except Exception as e:
        return ToolResult.fail(str(e))


READ_FILE_TOOL = Tool(
    name="read_file",
    description="Legge un file del workspace. Preferiscilo a cat/sed quando devi solo ispezionare contenuti.",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Percorso del file"},
            "start_line": {"type": "integer", "description": "Riga iniziale (1-indexed, opzionale)"},
            "end_line": {"type": "integer", "description": "Riga finale (opzionale)"}
        },
        "required": ["path"]
    },
    handler=read_file_handler
)

WRITE_FILE_TOOL = Tool(
    name="write_file",
    description="Scrive contenuto in un file. Usalo per file nuovi o rewrite completi. Crea backup automatico se il file esiste.",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Percorso del file"},
            "content": {"type": "string", "description": "Contenuto da scrivere"},
            "backup": {"type": "boolean", "description": "Crea backup (default: true)"},
            "dry_run": {"type": "boolean", "description": "If true, preview the write without changing the file"}
        },
        "required": ["path", "content"]
    },
    # Niente approvazione: la safety blocca già path esterni e file critici,
    # e i patti proteggono i path concordati con l'owner.
    requires_approval=False,
    handler=write_file_handler
)

EDIT_FILE_TOOL = Tool(
    name="edit_file",
    description="Modifica un file sostituendo old_string con new_string. Preferiscilo per patch mirate. Crea backup.",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Percorso del file"},
            "old_string": {"type": "string", "description": "Testo da cercare e sostituire"},
            "new_string": {"type": "string", "description": "Testo sostitutivo"},
            "replace_all": {"type": "boolean", "description": "Sostituisci tutte le occorrenze (default: false)"},
            "dry_run": {"type": "boolean", "description": "If true, preview the edit without changing the file"}
        },
        "required": ["path", "old_string", "new_string"]
    },
    requires_approval=False,
    handler=edit_file_handler
)

EDIT_LINES_TOOL = Tool(
    name="edit_lines",
    description=(
        "Modifica un file per numero di riga. Più semplice di edit_file. "
        "Leggi prima il file per vedere i numeri di riga, poi usa questo tool. "
        "Può sostituire righe, inserire nuove righe, o sostituire un range."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Percorso del file"},
            "line": {"type": "integer", "description": "Numero riga (1-indexed)"},
            "content": {"type": "string", "description": "Nuovo contenuto (può essere multi-riga con \\n)"},
            "end_line": {"type": "integer", "description": "Riga finale del range da sostituire (opzionale)"},
            "insert": {"type": "boolean", "description": "Se true, inserisce PRIMA della riga invece di sostituirla"},
            "dry_run": {"type": "boolean", "description": "If true, preview the line edit without changing the file"},
        },
        "required": ["path", "line", "content"]
    },
    requires_approval=False,
    handler=edit_lines_handler,
)

APPEND_FILE_TOOL = Tool(
    name="append_file",
    description="Aggiunge contenuto alla fine di un file. Usa per aggiungere righe senza riscrivere tutto.",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Percorso del file"},
            "content": {"type": "string", "description": "Contenuto da aggiungere alla fine"},
            "dry_run": {"type": "boolean", "description": "If true, preview the append without changing the file"},
        },
        "required": ["path", "content"]
    },
    requires_approval=False,
    handler=append_file_handler,
)
