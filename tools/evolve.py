"""
openvurp Tools — Self-Evolution

Permette all'agente di modificare i propri file workspace (SOUL.md, IDENTITY.md,
AGENTS.md, USER.md, TOOLS.md, MEMORY.md) e di vedere i cambiamenti al turno
successivo grazie al bootstrap loader stat-based.

Approccio a livelli: l'agente può evolversi, ma deve sempre notificare l'utente
quando modifica file critici (SOUL.md, IDENTITY.md).
"""

from __future__ import annotations

import os
from datetime import datetime

from core.bootstrap import normalize_workspace_filename, resolve_workspace_file
from core.tools import Tool


# File che l'agente può modificare
EVOLVABLE_FILES = {
    "SOUL.md", "IDENTITY.md", "AGENTS.md", "USER.md",
    "TOOLS.md", "MEMORY.md", "HEARTBEAT.md",
}

# File critici: richiedono notifica esplicita all'utente
CRITICAL_FILES = {"SOUL.md", "IDENTITY.md"}


def _get_openvurp_dir() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _normalize_evolvable_filename(filename: str) -> str:
    canonical = normalize_workspace_filename(filename)
    if canonical not in EVOLVABLE_FILES:
        return ""
    return canonical


def _evolve_handler(file: str, content: str, reason: str) -> str:
    """Handler per il tool evolve_self."""
    raw_filename = file.strip()
    filename = _normalize_evolvable_filename(raw_filename)
    reason = reason.strip()

    if not raw_filename:
        return "[ERROR] Parameter 'file' is required."

    if not filename:
        return (
            f"[ERROR] '{raw_filename}' is not an evolvable file. "
            f"File ammessi: {', '.join(sorted(EVOLVABLE_FILES))}"
        )

    if not content:
        return "[ERROR] Parameter 'content' is required — you cannot write an empty file."

    if not reason:
        return "[ERROR] Parameter 'reason' is required — you must explain why you are changing this file."

    openvurp_dir = _get_openvurp_dir()
    canonical_name, filepath = resolve_workspace_file(openvurp_dir, filename)
    actual_name = os.path.basename(filepath)
    display_name = (
        canonical_name
        if actual_name == canonical_name
        else f"{canonical_name} (file: {actual_name})"
    )

    # Leggi il contenuto attuale per confronto
    old_content = ""
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            old_content = f.read()
    except FileNotFoundError:
        pass

    if old_content == content:
        return f"[NOOP] {display_name} is already identical — no change needed."

    # Scrivi il nuovo contenuto
    try:
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)
    except OSError as e:
        return f"[ERROR] Cannot write {filename}: {e}"

    # Notifica
    is_critical = filename in CRITICAL_FILES
    notification = ""
    if is_critical:
        notification = (
            f"\n\n⚠️ NOTA: {filename} è un file critico (la tua anima/identità). "
            f"L'utente DEVE essere informato di questa modifica."
        )

    # Log della modifica
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    old_lines = len(old_content.split("\n")) if old_content else 0
    new_lines = len(content.split("\n"))

    # I file workspace vengono riletti ogni turno (a livelli).
    # NON serve riavviare — il cambiamento è già visibile al prossimo turno.
    restart_note = ""

    return (
        f"[EVOLUZIONE] {filename} aggiornato.\n"
        f"  File reale: {actual_name}\n"
        f"  Timestamp: {timestamp}\n"
        f"  Motivo: {reason}\n"
        f"  Prima: {old_lines} righe, {len(old_content)} chars\n"
        f"  Dopo: {new_lines} righe, {len(content)} chars"
        f"{restart_note}"
        f"{notification}"
    )


def _read_self_handler(file: str) -> str:
    """Handler per leggere un file workspace corrente."""
    filename = normalize_workspace_filename(file)

    if not filename:
        return "[ERROR] Parameter 'file' is required."

    if filename not in EVOLVABLE_FILES:
        return (
            f"[ERROR] '{filename}' is not a workspace file. "
            f"Allowed files: {', '.join(sorted(EVOLVABLE_FILES))}"
        )

    openvurp_dir = _get_openvurp_dir()
    _, filepath = resolve_workspace_file(openvurp_dir, filename)

    try:
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()
        return content
    except FileNotFoundError:
        return f"[INFO] {filename} non esiste ancora."
    except OSError as e:
        return f"[ERROR] Cannot read {filename}: {e}"


# ── Tool Definitions ──

EVOLVE_SELF_TOOL = Tool(
    name="evolve_self",
    description=(
        "Modifica un file workspace (SOUL.md, IDENTITY.md, AGENTS.md, USER.md, "
        "TOOLS.md, MEMORY.md, HEARTBEAT.md). Il cambiamento è attivo dal turno "
        "successivo. Per SOUL.md e IDENTITY.md, DEVI informare l'utente."
    ),
    parameters={
        "type": "object",
        "properties": {
            "file": {
                "type": "string",
                "description": "Nome del file da modificare (es: SOUL.md, IDENTITY.md)",
                "enum": sorted(EVOLVABLE_FILES),
            },
            "content": {
                "type": "string",
                "description": "Il nuovo contenuto completo del file",
            },
            "reason": {
                "type": "string",
                "description": "Perché stai modificando questo file — l'utente lo vedrà",
            },
        },
        "required": ["file", "content", "reason"],
    },
    handler=_evolve_handler,
)

READ_SELF_TOOL = Tool(
    name="read_self",
    description=(
        "Leggi il contenuto attuale di un file workspace "
        "(SOUL.md, IDENTITY.md, AGENTS.md, USER.md, TOOLS.md, MEMORY.md, "
        "HEARTBEAT.md). Useful to know who you are before evolving."
    ),
    parameters={
        "type": "object",
        "properties": {
            "file": {
                "type": "string",
                "description": "Name of the file to read",
                "enum": sorted(EVOLVABLE_FILES),
            },
        },
        "required": ["file"],
    },
    handler=_read_self_handler,
)
