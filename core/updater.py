"""
openvurp Core — Self-update

Aggiornamento autonomo dal repository git: controlla se ci sono novità,
applica SOLO fast-forward puliti (niente merge automatici rischiosi), fa uno
smoke-test e in caso di problemi torna indietro da solo. Pensato per girare
anche quando l'owner non c'è: se l'update non è sicuro, non lo applica.

Il riavvio riusa il sentinel `memory/.restart` già gestito dal resto del
runtime, così il tool `request_restart` dell'agente continua a funzionare.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

OPENVURP_DIR = Path(__file__).resolve().parent.parent
RESTART_SENTINEL = OPENVURP_DIR / "memory" / ".restart"

# Moduli importati nello smoke-test dopo un pull: se uno non importa,
# l'update è rotto e si fa rollback.
SMOKE_MODULES = ("config", "core.agent", "core.llm", "core.tools")


# ── git helpers ──────────────────────────────────────────────────────────

def _git(*args: str, timeout: int = 30) -> tuple[int, str, str]:
    """Esegue git nel workspace. Ritorna (returncode, stdout, stderr)."""
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(OPENVURP_DIR),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except FileNotFoundError:
        return 127, "", "git non installato"
    except subprocess.TimeoutExpired:
        return 124, "", "git timeout"


def is_git_repo() -> bool:
    rc, out, _ = _git("rev-parse", "--is-inside-work-tree")
    return rc == 0 and out == "true"


def current_revision() -> str:
    rc, out, _ = _git("rev-parse", "--short", "HEAD")
    return out if rc == 0 else ""


def working_tree_dirty() -> bool:
    """True se ci sono modifiche locali non committate (update non sicuro)."""
    rc, out, _ = _git("status", "--porcelain")
    return rc == 0 and bool(out.strip())


# ── pure: classificazione stato rispetto all'upstream ────────────────────

def classify_revisions(local: str, remote: str, base: str) -> str:
    """Stato del branch locale rispetto all'upstream.

    - up_to_date: nessuna novità
    - behind:     l'upstream è avanti e si può fast-forward (update disponibile)
    - ahead:      siamo avanti noi (commit locali non pushati)
    - diverged:   storie divergenti (serve intervento manuale, niente auto-merge)
    """
    if not local or not remote:
        return "unknown"
    if local == remote:
        return "up_to_date"
    if local == base:
        return "behind"
    if remote == base:
        return "ahead"
    return "diverged"


# ── check / apply ────────────────────────────────────────────────────────

def check_for_updates(fetch: bool = True) -> dict:
    """Controlla se l'upstream ha aggiornamenti. Non modifica nulla."""
    if not is_git_repo():
        return {"available": False, "status": "not_a_repo", "summary": "not a git repo"}
    if fetch:
        rc, _, err = _git("fetch", "--quiet", timeout=30)
        if rc != 0:
            return {"available": False, "status": "fetch_failed", "summary": err or "fetch failed"}

    rc_u, upstream, _ = _git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    if rc_u != 0 or not upstream:
        return {"available": False, "status": "no_upstream", "summary": "no upstream configured"}

    _, local, _ = _git("rev-parse", "@")
    _, remote, _ = _git("rev-parse", "@{u}")
    _, base, _ = _git("merge-base", "@", "@{u}")
    status = classify_revisions(local, remote, base)

    behind = 0
    if status == "behind":
        rc_c, out_c, _ = _git("rev-list", "--count", "HEAD..@{u}")
        if rc_c == 0 and out_c.isdigit():
            behind = int(out_c)

    summaries = {
        "up_to_date": "up to date",
        "behind": f"{behind} update commits available",
        "ahead": "you have unpushed local commits",
        "diverged": "diverged histories: manual merge needed",
        "unknown": "state undeterminable",
    }
    return {
        "available": status == "behind",
        "status": status,
        "behind": behind,
        "local": local[:7],
        "remote": remote[:7],
        "upstream": upstream,
        "summary": summaries.get(status, status),
    }


def _smoke_check() -> tuple[bool, str]:
    """Importa i moduli chiave in un sottoprocesso pulito. False se rotto."""
    code = "import importlib,sys\n" + "".join(
        f"importlib.import_module({m!r})\n" for m in SMOKE_MODULES
    ) + "print('ok')"
    try:
        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(OPENVURP_DIR), capture_output=True, text=True, timeout=60,
        )
        if proc.returncode == 0 and "ok" in proc.stdout:
            return True, ""
        return False, (proc.stderr.strip() or "smoke-test fallito")[:500]
    except Exception as exc:
        return False, str(exc)


def apply_update(smoke_test: bool = True) -> dict:
    """Applica l'update se sicuro: solo fast-forward, working tree pulito.

    Con smoke_test=True fa un import dei moduli chiave dopo il pull e, se
    qualcosa è rotto, torna automaticamente alla revisione precedente.
    """
    if not is_git_repo():
        return {"ok": False, "error": "not a git repo"}
    if working_tree_dirty():
        return {"ok": False, "error": "uncommitted local changes: update cancelled to avoid losing them"}

    info = check_for_updates(fetch=True)
    if info["status"] == "up_to_date":
        return {"ok": True, "updated": False, "summary": "up to date"}
    if not info["available"]:
        return {"ok": False, "error": f"unsafe update: {info['summary']}"}

    before = current_revision()
    rc, out, err = _git("merge", "--ff-only", "@{u}", timeout=60)
    if rc != 0:
        return {"ok": False, "error": err or out or "fast-forward failed"}

    if smoke_test:
        ok, detail = _smoke_check()
        if not ok:
            _git("reset", "--hard", before or "HEAD@{1}", timeout=60)
            return {
                "ok": False, "updated": False, "rolled_back": True,
                "error": f"update broken, rolled back to {before}: {detail}",
            }

    after = current_revision()
    _, changed, _ = _git("diff", "--stat", f"{before}..{after}")
    return {
        "ok": True, "updated": True, "from": before, "to": after,
        "summary": f"updated {before} → {after}",
        "changes": changed,
    }


# ── restart ──────────────────────────────────────────────────────────────

def request_restart(reason: str = "") -> None:
    """Scrive il sentinel di restart (consumato dal runtime/TUI/watcher)."""
    import time
    RESTART_SENTINEL.parent.mkdir(parents=True, exist_ok=True)
    RESTART_SENTINEL.write_text(f"{time.time()}\n{reason}\n", encoding="utf-8")


def restart_pending() -> bool:
    return RESTART_SENTINEL.exists()


def consume_restart() -> str:
    """Consuma il sentinel e ritorna il motivo (o '' se assente)."""
    if not RESTART_SENTINEL.exists():
        return ""
    try:
        text = RESTART_SENTINEL.read_text(encoding="utf-8")
        RESTART_SENTINEL.unlink()
    except OSError:
        return ""
    lines = [l for l in text.splitlines() if l.strip()]
    return lines[-1] if len(lines) > 1 else "restart"


def restart_in_place(argv: list[str] | None = None) -> None:
    """Sostituisce il processo corrente con uno nuovo (stesso terminale).

    NON ritorna se ha successo. Va chiamata DOPO aver chiuso curses, così il
    terminale resta in uno stato pulito. Carica il codice aggiornato perché è
    un processo Python fresco.
    """
    args = list(argv if argv is not None else sys.argv)
    prog = args[0] if args else ""
    rest = args[1:]

    # 1) App "congelata" (PyInstaller & co.): l'eseguibile È sys.executable,
    #    non c'è uno script da ridare all'interprete.
    if getattr(sys, "frozen", False):
        os.execv(sys.executable, [sys.executable, *rest])
        return

    # 2) Risolvi argv[0] a un file reale. Copre: path assoluto, nome su PATH,
    #    e la variante Windows senza estensione (...\Scripts\openvurp → .exe).
    target = prog
    if prog and not os.path.exists(target):
        if os.name == "nt" and os.path.exists(prog + ".exe"):
            target = prog + ".exe"
        else:
            import shutil
            found = shutil.which(prog)
            if found:
                target = found

    # 3) Su Windows i console-script sono launcher .exe NATIVI: vanno eseguiti
    #    direttamente. `python.exe <...\Scripts\openvurp>` fallirebbe ("can't
    #    open file"). Questo è il caso che rompeva il /restart su Windows.
    if os.name == "nt" and target.lower().endswith(".exe") and os.path.exists(target):
        os.execv(target, [target, *rest])
        return

    # 4) Ovunque altrove — Linux/macOS console-script (file con shebang python),
    #    `python main.py`, ecc.: re-exec con l'interprete corrente sul file
    #    risolto. Identico al comportamento storico quando argv[0] è già reale.
    if target and os.path.exists(target):
        os.execv(sys.executable, [sys.executable, target, *rest])
        return

    # 5) Ultima spiaggia: ripassa argv invariato all'interprete.
    os.execv(sys.executable, [sys.executable, *args])
