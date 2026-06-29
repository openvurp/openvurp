"""
openvurp Security — Integrity Checker

Verifica integrità dei file di openvurp: core, tools, plugins.
Rileva modifiche non autorizzate al codice dell'agente.
"""

from __future__ import annotations

import os
import json
import hashlib
import time
from dataclasses import dataclass
from typing import Optional
from pathlib import Path


@dataclass
class IntegrityReport:
    """Risultato di una verifica di integrità."""
    valid: bool
    total_files: int
    modified: list[str]
    missing: list[str]
    new_files: list[str]
    timestamp: float
    message: str


class IntegrityChecker:
    """
    Verifica integrità dei file openvurp.

    Crea un baseline (hash di tutti i file), poi verifica
    che nessun file sia stato modificato senza autorizzazione.

    Utile per:
    - Verificare che plugin malevoli non abbiano modificato il core
    - Controllare che l'LLM non abbia alterato file critici
    - Audit di sicurezza
    """

    BASELINE_FILE = ".integrity_baseline.json"

    # Pattern di file da controllare
    MONITORED_PATTERNS = [
        "core/*.py",
        "core/security/*.py",
        "tools/*.py",
        "channels/*.py",
        "main.py",
        "agent.py",
        "config.py",
        "dashboard.py",
        "ears.py",
        "eyes.py",
    ]

    # File/directory da escludere
    EXCLUDE = {
        "__pycache__", ".pyc", ".pyo", ".git",
        "memory", "logs", "oldvurp",
        ".integrity_baseline.json",
    }

    def __init__(self, openvurp_dir: str):
        self.openvurp_dir = openvurp_dir
        self._baseline_path = os.path.join(openvurp_dir, self.BASELINE_FILE)

    def create_baseline(self) -> int:
        """
        Crea baseline dei file. Ritorna numero di file registrati.
        Va eseguito dopo installazione o aggiornamento.
        """
        hashes = self._compute_hashes()

        baseline = {
            "created": time.time(),
            "openvurp_dir": self.openvurp_dir,
            "files": hashes,
        }

        with open(self._baseline_path, "w", encoding="utf-8") as f:
            json.dump(baseline, f, indent=2)

        try:
            os.chmod(self._baseline_path, 0o600)
        except OSError:
            pass

        return len(hashes)

    def verify(self) -> IntegrityReport:
        """
        Verifica integrità confrontando con baseline.
        """
        if not os.path.exists(self._baseline_path):
            return IntegrityReport(
                valid=False,
                total_files=0,
                modified=[],
                missing=[],
                new_files=[],
                timestamp=time.time(),
                message="Nessun baseline trovato. Esegui create_baseline() prima.",
            )

        # Carica baseline
        with open(self._baseline_path, "r", encoding="utf-8") as f:
            baseline = json.load(f)

        stored_hashes = baseline.get("files", {})
        current_hashes = self._compute_hashes()

        modified = []
        missing = []
        new_files = []

        # File modificati o mancanti
        for filepath, stored_hash in stored_hashes.items():
            if filepath not in current_hashes:
                missing.append(filepath)
            elif current_hashes[filepath] != stored_hash:
                modified.append(filepath)

        # File nuovi
        for filepath in current_hashes:
            if filepath not in stored_hashes:
                new_files.append(filepath)

        valid = len(modified) == 0 and len(missing) == 0

        parts = []
        if valid and not new_files:
            parts.append(f"Integrità OK: {len(stored_hashes)} file verificati.")
        else:
            if modified:
                parts.append(f"MODIFICATI: {', '.join(modified)}")
            if missing:
                parts.append(f"MANCANTI: {', '.join(missing)}")
            if new_files:
                parts.append(f"NUOVI: {', '.join(new_files)}")

        return IntegrityReport(
            valid=valid,
            total_files=len(stored_hashes),
            modified=modified,
            missing=missing,
            new_files=new_files,
            timestamp=time.time(),
            message=" | ".join(parts),
        )

    def verify_file(self, filepath: str) -> tuple[bool, str]:
        """Verifica un singolo file."""
        if not os.path.exists(self._baseline_path):
            return False, "Nessun baseline."

        with open(self._baseline_path, "r", encoding="utf-8") as f:
            baseline = json.load(f)

        stored_hashes = baseline.get("files", {})
        rel_path = os.path.relpath(filepath, self.openvurp_dir)

        if rel_path not in stored_hashes:
            return False, f"File non nel baseline: {rel_path}"

        current_hash = self._hash_file(filepath)
        if current_hash != stored_hashes[rel_path]:
            return False, f"File modificato: {rel_path}"

        return True, "OK"

    def update_baseline(self, filepath: str):
        """Aggiorna il baseline per un singolo file (dopo modifica autorizzata)."""
        if not os.path.exists(self._baseline_path):
            self.create_baseline()
            return

        with open(self._baseline_path, "r", encoding="utf-8") as f:
            baseline = json.load(f)

        rel_path = os.path.relpath(filepath, self.openvurp_dir)

        if os.path.exists(filepath):
            baseline["files"][rel_path] = self._hash_file(filepath)
        else:
            baseline["files"].pop(rel_path, None)

        with open(self._baseline_path, "w", encoding="utf-8") as f:
            json.dump(baseline, f, indent=2)

    # ── Internals ──

    def _compute_hashes(self) -> dict[str, str]:
        """Calcola hash SHA256 di tutti i file monitorati."""
        hashes = {}

        for pattern in self.MONITORED_PATTERNS:
            # Risolvi pattern glob manualmente
            if "*" in pattern:
                dir_part = os.path.dirname(pattern)
                dir_path = os.path.join(self.openvurp_dir, dir_part)
                if not os.path.isdir(dir_path):
                    continue
                ext = pattern.split("*")[-1] if "*" in pattern else ""
                for fname in os.listdir(dir_path):
                    if ext and not fname.endswith(ext):
                        continue
                    if any(excl in fname for excl in self.EXCLUDE):
                        continue
                    full = os.path.join(dir_path, fname)
                    if os.path.isfile(full):
                        rel = os.path.relpath(full, self.openvurp_dir)
                        hashes[rel] = self._hash_file(full)
            else:
                full = os.path.join(self.openvurp_dir, pattern)
                if os.path.isfile(full):
                    rel = os.path.relpath(full, self.openvurp_dir)
                    hashes[rel] = self._hash_file(full)

        return hashes

    @staticmethod
    def _hash_file(filepath: str) -> str:
        """SHA256 di un file."""
        h = hashlib.sha256()
        try:
            with open(filepath, "rb") as f:
                while True:
                    chunk = f.read(8192)
                    if not chunk:
                        break
                    h.update(chunk)
            return h.hexdigest()
        except Exception:
            return "ERROR"
