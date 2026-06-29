#!/usr/bin/env python3
"""
openvurp Watcher — Auto-restart supervisor.

Avvia main.py come sottoprocesso e lo riavvia quando:
1. File .py vengono modificati (evolve_self, aggiornamenti manuali)
2. File workspace .md vengono modificati (IDENTITY.md, AGENTS.md, etc.)
3. Il file sentinella memory/.restart viene creato (restart esplicito)
4. Il processo crasha

Uso:
    python watcher.py              # Avvia openvurp con auto-restart
    python watcher.py --no-telegram  # Passa argomenti a main.py
"""

import os
import sys
import time
import signal
import subprocess
from pathlib import Path

OPENVURP_DIR = os.path.dirname(os.path.abspath(__file__))
RESTART_SENTINEL = os.path.join(OPENVURP_DIR, "memory", ".restart")
RESTARTED_SENTINEL = os.path.join(OPENVURP_DIR, "memory", ".restarted")

# File e pattern da monitorare
WATCH_EXTENSIONS = {".py", ".md"}
WATCH_DIRS = [
    OPENVURP_DIR,
    os.path.join(OPENVURP_DIR, "core"),
    os.path.join(OPENVURP_DIR, "tools"),
    os.path.join(OPENVURP_DIR, "channels"),
    os.path.join(OPENVURP_DIR, "plugins"),
    os.path.join(OPENVURP_DIR, "skills"),
]

# File da ignorare (cambiamenti frequenti non rilevanti)
IGNORE_PATTERNS = {
    "__pycache__",
    ".pyc",
    "watcher.py",  # Non riavviare per modifiche a se stesso
    "memory/sessions",
    "memory/cache",
    "memory/audit",
    "logs/",
}

# File .md del workspace — riletti ogni turno, non serve riavviare
WORKSPACE_MD_FILES = {
    "SOUL.md", "IDENTITY.md", "USER.md", "AGENTS.md",
    "TOOLS.md", "MEMORY.md", "HEARTBEAT.md", "BOOTSTRAP.md",
}

# Tempo minimo tra riavvii (evita loop infiniti)
MIN_RESTART_INTERVAL = 5  # secondi
# Intervallo di polling per modifiche
POLL_INTERVAL = 2  # secondi
# Grazia dopo avvio prima di monitorare (lascia che l'agente si stabilizzi)
STARTUP_GRACE = 10  # secondi


def should_ignore(filepath: str) -> bool:
    """Controlla se un file va ignorato."""
    for pattern in IGNORE_PATTERNS:
        if pattern in filepath:
            return True
    # I file .md del workspace vengono riletti ogni turno — no restart
    basename = os.path.basename(filepath)
    if basename in WORKSPACE_MD_FILES:
        return True
    return False


def scan_files() -> dict[str, float]:
    """Scansiona i file monitorati e restituisce {path: mtime}."""
    snapshot = {}
    for watch_dir in WATCH_DIRS:
        if not os.path.isdir(watch_dir):
            continue
        try:
            if os.path.abspath(watch_dir) == os.path.abspath(OPENVURP_DIR):
                for entry in os.scandir(watch_dir):
                    if not entry.is_file():
                        continue
                    ext = os.path.splitext(entry.name)[1].lower()
                    if ext not in WATCH_EXTENSIONS:
                        continue
                    if should_ignore(entry.path):
                        continue
                    try:
                        snapshot[entry.path] = entry.stat().st_mtime
                    except OSError:
                        pass
                continue

            for root, dirs, files in os.walk(watch_dir):
                dirs[:] = [
                    d for d in dirs
                    if not should_ignore(os.path.join(root, d))
                ]
                for filename in files:
                    path = os.path.join(root, filename)
                    ext = os.path.splitext(filename)[1].lower()
                    if ext not in WATCH_EXTENSIONS:
                        continue
                    if should_ignore(path):
                        continue
                    try:
                        snapshot[path] = os.path.getmtime(path)
                    except OSError:
                        pass
        except OSError:
            pass
    return snapshot


def check_sentinel() -> bool:
    """Controlla se il file sentinella di restart esiste."""
    if os.path.exists(RESTART_SENTINEL):
        try:
            os.remove(RESTART_SENTINEL)
        except OSError:
            pass
        return True
    return False


def detect_changes(old_snapshot: dict, new_snapshot: dict) -> list[str]:
    """Trova file modificati, aggiunti o rimossi."""
    changes = []

    # File modificati o nuovi
    for path, mtime in new_snapshot.items():
        old_mtime = old_snapshot.get(path)
        if old_mtime is None or mtime > old_mtime:
            changes.append(path)

    # File rimossi
    for path in old_snapshot:
        if path not in new_snapshot:
            changes.append(path)

    return changes


class OpenvurpWatcher:
    """Supervisor che gestisce il ciclo di vita di openvurp."""

    def __init__(self, extra_args: list[str] = None):
        self.extra_args = extra_args or []
        self.process: subprocess.Popen | None = None
        self.running = True
        self.restart_count = 0
        self.last_restart = 0.0

        # Gestisci SIGINT/SIGTERM per shutdown pulito
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

    def _signal_handler(self, signum, frame):
        """Shutdown pulito su Ctrl+C."""
        print("\n[watcher] Shutdown richiesto...")
        self.running = False
        self._stop_process()

    def _stop_process(self):
        """Ferma il processo openvurp."""
        if self.process and self.process.poll() is None:
            print("[watcher] Fermo openvurp...")
            try:
                # Manda SIGINT per shutdown graceful
                if sys.platform == "win32":
                    self.process.terminate()
                else:
                    self.process.send_signal(signal.SIGINT)

                # Aspetta fino a 5 secondi
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    print("[watcher] openvurp non risponde, termino forzatamente...")
                    self.process.kill()
                    self.process.wait(timeout=3)
            except Exception as e:
                print(f"[watcher] Errore stop: {e}")

    def _mark_restarted(self, reason: str = ""):
        """Scrive il file .restarted così main.py sa che è un riavvio, non un primo avvio."""
        os.makedirs(os.path.dirname(RESTARTED_SENTINEL), exist_ok=True)
        with open(RESTARTED_SENTINEL, "w", encoding="utf-8") as f:
            f.write(f"{time.time()}\n{reason}\n")

    def _start_process(self) -> subprocess.Popen:
        """Avvia main.py come sottoprocesso."""
        cmd = [sys.executable, os.path.join(OPENVURP_DIR, "main.py")] + self.extra_args
        print(f"[watcher] Avvio openvurp... (restart #{self.restart_count})")
        env = dict(os.environ)
        env["OPENVURP_UNDER_WATCHER"] = "1"  # /restart usa la sentinella, non execv
        proc = subprocess.Popen(
            cmd,
            cwd=OPENVURP_DIR,
            # Passa stdin/stdout/stderr direttamente al terminale
            stdin=sys.stdin,
            stdout=sys.stdout,
            stderr=sys.stderr,
            env=env,
        )
        self.last_restart = time.time()
        return proc

    def _can_restart(self) -> bool:
        """Evita restart troppo ravvicinati."""
        elapsed = time.time() - self.last_restart
        if elapsed < MIN_RESTART_INTERVAL:
            print(f"[watcher] Troppo presto per riavviare ({elapsed:.1f}s < {MIN_RESTART_INTERVAL}s)")
            return False
        return True

    def run(self):
        """Loop principale del watcher."""
        print("=" * 50)
        print("  openvurp Watcher — Auto-restart abilitato")
        print("  Ctrl+C per fermare")
        print("=" * 50)
        print()

        # Avvio iniziale
        self.process = self._start_process()
        time.sleep(STARTUP_GRACE)

        # Snapshot iniziale dei file
        file_snapshot = scan_files()

        while self.running:
            # 1. Controlla se il processo è ancora vivo
            if self.process.poll() is not None:
                exit_code = self.process.returncode
                if exit_code == 0:
                    print("\n[watcher] openvurp terminato normalmente.")
                    break

                # Exit code 42 = restart richiesto (non è un crash)
                if exit_code == 42:
                    print("\n[watcher] Restart richiesto da openvurp")
                else:
                    print(f"\n[watcher] openvurp crashato (exit code: {exit_code})")

                if self._can_restart():
                    self.restart_count += 1
                    print("[watcher] Riavvio in corso...")
                    self._mark_restarted(f"exit_code={exit_code}")
                    time.sleep(2)
                    self.process = self._start_process()
                    time.sleep(STARTUP_GRACE)
                    file_snapshot = scan_files()
                else:
                    time.sleep(MIN_RESTART_INTERVAL)
                continue

            # 2. Controlla sentinella di restart
            if check_sentinel():
                print("\n[watcher] Restart richiesto via sentinella")
                if self._can_restart():
                    self._stop_process()
                    self.restart_count += 1
                    self._mark_restarted("sentinel")
                    self.process = self._start_process()
                    time.sleep(STARTUP_GRACE)
                    file_snapshot = scan_files()
                continue

            # 3. Controlla modifiche ai file
            new_snapshot = scan_files()
            changes = detect_changes(file_snapshot, new_snapshot)
            if changes:
                # Filtra solo cambiamenti significativi
                significant = [c for c in changes if not should_ignore(c)]
                if significant and self._can_restart():
                    names = [os.path.relpath(c, OPENVURP_DIR) for c in significant[:5]]
                    print(f"\n[watcher] File modificati: {', '.join(names)}")
                    print("[watcher] Riavvio per applicare le modifiche...")
                    self._stop_process()
                    self.restart_count += 1
                    self._mark_restarted(f"file_change: {', '.join(names)}")
                    self.process = self._start_process()
                    time.sleep(STARTUP_GRACE)

                file_snapshot = new_snapshot
                continue

            # Polling
            time.sleep(POLL_INTERVAL)

        print("[watcher] Bye.")


def request_restart(reason: str = ""):
    """Utility: crea il file sentinella per richiedere un restart.

    Può essere chiamata da altri moduli openvurp (es. evolve_self) per
    triggerare un riavvio senza conoscere i dettagli del watcher.
    """
    os.makedirs(os.path.dirname(RESTART_SENTINEL), exist_ok=True)
    with open(RESTART_SENTINEL, "w", encoding="utf-8") as f:
        f.write(f"{time.time()}\n{reason}\n")


if __name__ == "__main__":
    # Passa tutti gli argomenti dopo watcher.py a main.py
    extra = sys.argv[1:]
    watcher = OpenvurpWatcher(extra_args=extra)
    watcher.run()
