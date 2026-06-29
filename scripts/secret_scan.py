#!/usr/bin/env python3
"""Secret scanner locale per openvurp.

Non usa dipendenze esterne e redige sempre il valore trovato.
Exit code 1 quando trova segreti probabili.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path


DEFAULT_EXCLUDED_DIRS = {
    ".git",
    ".agents",
    ".backups",
    ".claude",
    ".reset_baseline",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    "memory",
    "memory/runtime",
    "node_modules",
}

MAX_FILE_BYTES = 500_000

DEFAULT_EXCLUDED_SUFFIXES = {
    ".exe",
    ".dll",
    ".zip",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".ico",
    ".pdf",
    ".mp3",
    ".wav",
    ".mp4",
    ".pyc",
}


@dataclass(frozen=True)
class Rule:
    name: str
    pattern: re.Pattern[str]


RULES = [
    Rule("telegram_bot_token", re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b")),
    Rule("openai_key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    Rule("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b")),
    Rule("groq_key", re.compile(r"\bgsk_[A-Za-z0-9_-]{20,}\b")),
    Rule(
        "generic_secret_assignment",
        re.compile(
            r"(?i)\b(api[_-]?key|token|secret|password|passwd)\b"
            r"\s*[:=]\s*['\"]([^'\"]{20,})['\"]"
        ),
    ),
]


ALLOWLIST_VALUES = {
    "your-token-here",
    "your-api-key-here",
    "not-needed",
    "example",
}


def _is_excluded(path: Path, root: Path) -> bool:
    rel = path.relative_to(root).as_posix()
    parts = rel.split("/")
    for idx in range(len(parts)):
        subpath = "/".join(parts[: idx + 1])
        if subpath in DEFAULT_EXCLUDED_DIRS:
            return True
    if path.suffix.lower() in DEFAULT_EXCLUDED_SUFFIXES:
        return True
    try:
        return path.is_file() and path.stat().st_size > MAX_FILE_BYTES
    except OSError:
        return True


def _redact(value: str) -> str:
    value = value.strip()
    if len(value) <= 10:
        return "***"
    return f"{value[:4]}...{value[-4:]}"


def _iter_files(root: Path):
    for current_root, dirs, files in os.walk(root):
        current = Path(current_root)
        dirs[:] = [
            d for d in dirs
            if not _is_excluded(current / d, root)
        ]
        for filename in files:
            path = current / filename
            if not _is_excluded(path, root):
                yield path


def _scan_file(path: Path, root: Path) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return []

    findings: list[str] = []
    rel = path.relative_to(root).as_posix()
    for line_no, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("#") and "example" in stripped.lower():
            continue
        for rule in RULES:
            for match in rule.pattern.finditer(line):
                value = match.group(2) if rule.name == "generic_secret_assignment" else match.group(0)
                if value.lower() in ALLOWLIST_VALUES or "..." in value:
                    continue
                findings.append(f"{rel}:{line_no}: {rule.name}: {_redact(value)}")
    return findings


def _env_is_gitignored(root: Path) -> bool:
    """True se .env è coperto dal .gitignore del progetto."""
    gitignore = root / ".gitignore"
    try:
        lines = {line.strip() for line in gitignore.read_text(encoding="utf-8").splitlines()}
    except OSError:
        return False
    return bool({".env", "/.env", ".env.*"} & lines)


def scan(root: Path) -> tuple[list[str], list[str]]:
    """Returns (findings, notes).

    Il file .env di root è il posto PRESCRITTO per i segreti locali: se è
    gitignorato non è un finding (non verrà mai pushato), ma una nota.
    Se NON è gitignorato resta un finding a tutti gli effetti.
    """
    root = root.resolve()
    findings: list[str] = []
    notes: list[str] = []
    env_ok = _env_is_gitignored(root)

    for path in _iter_files(root):
        file_findings = _scan_file(path, root)
        if not file_findings:
            continue
        if env_ok and path.name == ".env" and path.parent == root:
            notes.append(
                f".env contiene {len(file_findings)} segreto/i — ok, è gitignorato; "
                "non committarlo mai"
            )
            continue
        findings.extend(file_findings)
    return findings, notes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scansiona il repo per segreti probabili.")
    parser.add_argument("path", nargs="?", default=".", help="Directory da scansionare")
    args = parser.parse_args(argv)

    findings, notes = scan(Path(args.path))
    if findings:
        print("Secret scan failed:")
        for finding in findings:
            print(f"- {finding}")
        return 1

    print("Secret scan OK")
    for note in notes:
        print(f"  nota: {note}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
