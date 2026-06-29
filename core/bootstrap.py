"""
openvurp Core — Bootstrap Context Loader

Carica i file workspace (SOUL.md, IDENTITY.md, AGENTS.md, USER.md, TOOLS.md,
MEMORY.md, HEARTBEAT.md, BOOTSTRAP.md) freschi da disco ad ogni turno.

Approccio a livelli:
- Rilegge da disco ogni turno (no cache statica, ma identity-check via stat)
- Size cap per singolo file e per totale bootstrap
- Filtraggio per tipo di sessione (main, group, subagent, cron)
- I file modificati dall'agente sono disponibili al turno successivo
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timedelta
from dataclasses import dataclass
from typing import Optional


# ── Costanti ──

# File workspace riconosciuti
BOOTSTRAP_FILENAMES = [
    "AGENTS.md",
    "SOUL.md",
    "USER.md",
    "IDENTITY.md",
    "TOOLS.md",
    "MEMORY.md",
    "HEARTBEAT.md",
    "BOOTSTRAP.md",
]

# Compatibilità con i nomi file legacy del workspace.
# In particolare il bootstrap storico generava `soul.md` minuscolo.
WORKSPACE_FILE_ALIASES = {
    name: tuple(dict.fromkeys([name, name.lower()]))
    for name in BOOTSTRAP_FILENAMES
}

# File letti all'inizio di qualunque sessione "normale".
SESSION_START_ALLOWLIST = {"AGENTS.md", "SOUL.md", "USER.md", "IDENTITY.md", "TOOLS.md"}

# File ammessi nelle chat di GRUPPO: contesto pubblico, visibile anche a
# estranei → niente USER.md (profilo privato dell'owner). Identità/voce/metodo
# operativo sì, profilo dell'owner no.
GROUP_ALLOWLIST = {"AGENTS.md", "SOUL.md", "IDENTITY.md", "TOOLS.md"}

# File ammessi solo in sessioni principali (DM con l'utente)
MAIN_SESSION_ONLY = {"MEMORY.md", "BOOTSTRAP.md"}

# File letti solo nel loop heartbeat
HEARTBEAT_ONLY = {"HEARTBEAT.md"}

# File mancanti che vale comunque la pena segnalare nel contesto
VISIBLE_MISSING_FILES = {"AGENTS.md", "SOUL.md", "USER.md", "IDENTITY.md", "TOOLS.md", "MEMORY.md"}

# Size caps
MAX_SINGLE_FILE_CHARS = 50_000       # 50K chars per singolo file
MAX_TOTAL_BOOTSTRAP_CHARS = 150_000  # 150K chars totale per tutti i file bootstrap


def normalize_workspace_filename(filename: str) -> str:
    """Normalizza un nome file workspace alla forma canonica."""
    trimmed = filename.strip()
    if not trimmed:
        return ""

    needle = trimmed.lower()
    for canonical, aliases in WORKSPACE_FILE_ALIASES.items():
        if needle == canonical.lower():
            return canonical
        if any(needle == alias.lower() for alias in aliases):
            return canonical
    return trimmed


def iter_workspace_aliases(filename: str) -> tuple[str, ...]:
    """Restituisce tutti gli alias compatibili per un file workspace."""
    canonical = normalize_workspace_filename(filename)
    aliases = WORKSPACE_FILE_ALIASES.get(canonical)
    if aliases:
        return aliases
    return (canonical,) if canonical else tuple()


def resolve_workspace_file(workspace_dir: str, filename: str) -> tuple[str, str]:
    """Risolvi il path reale di un file workspace gestendo alias e case legacy."""
    canonical = normalize_workspace_filename(filename)
    aliases = iter_workspace_aliases(canonical)

    for candidate in aliases:
        path = os.path.join(workspace_dir, candidate)
        if os.path.exists(path):
            return canonical, path

    try:
        existing_entries = {
            entry.lower(): entry
            for entry in os.listdir(workspace_dir)
        }
    except OSError:
        existing_entries = {}

    for candidate in aliases:
        actual = existing_entries.get(candidate.lower())
        if actual:
            return canonical, os.path.join(workspace_dir, actual)

    default_name = aliases[0] if aliases else canonical
    return canonical, os.path.join(workspace_dir, default_name)


@dataclass
class BootstrapFile:
    """Un file workspace caricato."""
    name: str
    path: str
    content: str
    missing: bool = False
    truncated: bool = False


@dataclass
class _CachedFile:
    """Cache entry per un singolo file, invalidata quando il file cambia su disco."""
    content: str
    mtime_ns: int
    size: int
    ino: int


class BootstrapLoader:
    """Carica i file workspace freschi da disco ad ogni turno.

    Usa stat-based identity caching: se il file non è cambiato (stessa
    dimensione, mtime, inode), restituisce il contenuto dalla cache.
    Se il file è stato modificato (dall'agente o dall'utente), rilegge da disco.
    """

    def __init__(self, workspace_dir: str):
        self.workspace_dir = workspace_dir
        self._cache: dict[str, _CachedFile] = {}

    def load_all(
        self,
        session_type: str = "main",
        max_per_file: int = MAX_SINGLE_FILE_CHARS,
        max_total: int = MAX_TOTAL_BOOTSTRAP_CHARS,
    ) -> list[BootstrapFile]:
        """Carica tutti i file bootstrap, filtrando per tipo sessione.

        Args:
            session_type: "main" (DM), "group" (chat di gruppo), "subagent", "cron", "heartbeat"
            max_per_file: max chars per singolo file
            max_total: max chars totale

        Returns:
            Lista di BootstrapFile con contenuto caricato
        """
        # Determina quali file caricare in base al tipo sessione
        if session_type == "heartbeat":
            allowed_names = HEARTBEAT_ONLY
        elif session_type == "group":
            allowed_names = GROUP_ALLOWLIST
        elif session_type in ("subagent", "cron"):
            allowed_names = SESSION_START_ALLOWLIST
        else:
            # main: file standard + memoria lunga privata + bootstrap one-shot
            allowed_names = SESSION_START_ALLOWLIST | MAIN_SESSION_ONLY

        files: list[BootstrapFile] = []
        total_chars = 0

        for name in BOOTSTRAP_FILENAMES:
            if name not in allowed_names:
                continue

            canonical_name, filepath = resolve_workspace_file(self.workspace_dir, name)
            content = self._read_with_cache(filepath)

            if content is None:
                files.append(BootstrapFile(
                    name=canonical_name, path=filepath, content="", missing=True
                ))
                continue

            # Size cap per file
            truncated = False
            if len(content) > max_per_file:
                content = content[:max_per_file] + "\n\n[... troncato — file troppo grande ...]"
                truncated = True

            # Budget totale
            if total_chars + len(content) > max_total:
                remaining = max_total - total_chars
                if remaining > 500:
                    content = content[:remaining] + "\n\n[... troncato per budget totale ...]"
                    truncated = True
                else:
                    # Non c'è spazio, skip
                    continue

            if not content.strip() and not truncated:
                continue

            total_chars += len(content)
            files.append(BootstrapFile(
                name=canonical_name, path=filepath, content=content,
                missing=False, truncated=truncated
            ))

        if session_type == "main":
            files.extend(
                self._load_daily_memory_logs(
                    max_per_file=max_per_file,
                    max_total=max_total,
                    used_chars=total_chars,
                )
            )

        return files

    def _load_daily_memory_logs(
        self,
        max_per_file: int,
        max_total: int,
        used_chars: int,
    ) -> list[BootstrapFile]:
        """Carica oggi + ieri in memory/YYYY-MM-DD.md se presenti."""
        files: list[BootstrapFile] = []
        budget_left = max_total - used_chars
        if budget_left <= 0:
            return files

        memory_dir = os.path.join(self.workspace_dir, "memory")
        today = datetime.now().date()
        dates = [today, today - timedelta(days=1)]

        for day in dates:
            filename = f"{day.isoformat()}.md"
            path = os.path.join(memory_dir, filename)
            content = self._read_with_cache(path)
            if content is None or not content.strip():
                continue

            truncated = False
            if len(content) > max_per_file:
                content = content[:max_per_file] + "\n\n[... troncato — file troppo grande ...]"
                truncated = True

            if len(content) > budget_left:
                if budget_left <= 500:
                    break
                content = content[:budget_left] + "\n\n[... troncato per budget totale ...]"
                truncated = True

            files.append(BootstrapFile(
                name=f"memory/{filename}",
                path=path,
                content=content,
                missing=False,
                truncated=truncated,
            ))
            budget_left -= len(content)
            if budget_left <= 0:
                break

        return files

    def _read_with_cache(self, filepath: str) -> Optional[str]:
        """Legge un file con stat-based caching.

        Se il file non è cambiato su disco (stesso mtime, size, inode),
        restituisce dalla cache. Altrimenti rilegge.
        """
        try:
            st = os.stat(filepath)
        except OSError:
            # File non esiste
            self._cache.pop(filepath, None)
            return None

        # Controlla cache
        cached = self._cache.get(filepath)
        if cached is not None:
            if (cached.mtime_ns == st.st_mtime_ns and
                cached.size == st.st_size and
                cached.ino == st.st_ino):
                return cached.content

        # Rileggi da disco
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                content = f.read()
        except OSError:
            self._cache.pop(filepath, None)
            return None

        # Aggiorna cache
        self._cache[filepath] = _CachedFile(
            content=content,
            mtime_ns=st.st_mtime_ns,
            size=st.st_size,
            ino=st.st_ino,
        )
        return content

    def invalidate(self, filename: str):
        """Invalida la cache per un file specifico (dopo che l'agente lo modifica)."""
        canonical = normalize_workspace_filename(filename)
        aliases = iter_workspace_aliases(canonical)

        for alias in aliases:
            self._cache.pop(os.path.join(self.workspace_dir, alias), None)

        try:
            entries = os.listdir(self.workspace_dir)
        except OSError:
            entries = []
        alias_lowers = {alias.lower() for alias in aliases}
        for entry in entries:
            if entry.lower() in alias_lowers:
                self._cache.pop(os.path.join(self.workspace_dir, entry), None)

    def invalidate_all(self):
        """Invalida tutta la cache."""
        self._cache.clear()

    def build_project_context(
        self,
        files: list[BootstrapFile],
    ) -> str:
        """Costruisce la sezione "Project Context" da iniettare nel system prompt.

        Approccio a livelli: i file workspace vengono iniettati come contesto separato
        alla fine del system prompt, non mescolati con le istruzioni tecniche.
        """
        loaded = [f for f in files if not f.missing and f.content.strip()]
        missing = [f.name for f in files if f.missing and f.name in VISIBLE_MISSING_FILES]
        if not loaded and not missing:
            return ""

        has_soul = any(f.name == "SOUL.md" for f in loaded)

        lines = [
            "# Project Context",
            "",
            "I seguenti file workspace sono già stati caricati freschi da disco per questa sessione.",
            "Usali come contesto autorevole. Non serve rileggerli manualmente a meno che l'utente lo chieda o ti serva un approfondimento reale.",
        ]

        if has_soul:
            lines.append(
                "SOUL.md è presente: incarna la sua persona e il suo tono. "
                "Evita risposte rigide o generiche; segui la sua guida."
            )

        lines.append("")

        for name in missing:
            lines.append(f"[File workspace mancante: {name}]")
        if missing:
            lines.append("")

        for f in loaded:
            lines.append(f"## {f.name}")
            lines.append("")
            lines.append(f.content)
            if f.truncated:
                lines.append(f"[File troncato da {f.name}]")
            lines.append("")

        return "\n".join(lines)


def resolve_session_type(source: str, sender: str, chat_type: str = "") -> str:
    """Determina il tipo di sessione in base a source, sender e chat_type.

    Args:
        chat_type: tipo di chat del canale ("group"/"supergroup" per i gruppi).
            È il segnale autorevole: il `sender` è il nome di una persona, non
            contiene "group", quindi senza questo i gruppi finivano per sbaglio
            in sessione "main" (e ricevevano memoria/profilo privati dell'owner).

    Returns:
        "main" per CLI e DM, "group" per chat di gruppo,
        "subagent" per sub-agenti, "heartbeat" per heartbeat
    """
    if source == "heartbeat":
        return "heartbeat"
    if source == "subagent":
        return "subagent"
    if source == "cron":
        return "cron"
    # Chat di gruppo (qualsiasi canale): contesto pubblico/ridotto, mai privato.
    if (chat_type or "").lower() in ("group", "supergroup"):
        return "group"
    if source == "cli":
        return "main"
    # Fallback storico: alcuni canali codificano il gruppo nel sender/source.
    if "group" in sender.lower() or "group" in source.lower():
        return "group"
    # DM su canale esterno
    return "main"
