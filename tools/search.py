"""
openvurp Tool — Search

Grep, glob, find con regex e filetype filter.
"""

from __future__ import annotations

import os
import re
import fnmatch

from core.tools import Tool, ToolResult, ErrorType


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

        for root, dirs, files in os.walk(path):
            # Skip hidden dirs and common junk
            dirs[:] = [d for d in dirs if not d.startswith('.') and d not in
                       ('node_modules', '__pycache__', '.git', 'venv', '.venv')]

            for fname in files:
                if file_pattern and not fnmatch.fnmatch(fname, file_pattern):
                    continue

                fpath = os.path.join(root, fname)
                try:
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
            if len(results) >= max_results:
                break

        if not results:
            return ToolResult.ok(f"No match for '{pattern}' in {path} ({searched} files searched)")

        header = f"[{len(results)} match in {searched} file]\n"
        return ToolResult.ok(header + "\n".join(results))

    except Exception as e:
        return ToolResult.fail(str(e))


def glob_handler(pattern: str, path: str = ".") -> ToolResult:
    """Cerca file per pattern glob."""
    try:
        results = []
        for root, dirs, files in os.walk(path):
            dirs[:] = [d for d in dirs if not d.startswith('.') and d not in
                       ('node_modules', '__pycache__', '.git', 'venv', '.venv')]

            for fname in files:
                if fnmatch.fnmatch(fname, pattern):
                    fpath = os.path.relpath(os.path.join(root, fname), path)
                    size = os.path.getsize(os.path.join(root, fname))
                    results.append(f"{fpath}  ({size} B)")

            if len(results) >= 200:
                break

        if not results:
            return ToolResult.ok(f"No file matches '{pattern}' in {path}")

        return ToolResult.ok(f"[{len(results)} file]\n" + "\n".join(results))

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
    description="Cerca file per nome o pattern nel workspace. Preferiscilo a shell find/rg --files per discovery file.",
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
