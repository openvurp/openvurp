"""
openvurp Core — Progetti

Obiettivi a lungo termine che sopravvivono ai riavvii. Un progetto ha
sempre UN prossimo passo concreto: è quello che l'agente avanza nei
cicli autonomi e tiene presente durante il giorno.

I cicli (heartbeat) danno il ritmo; i progetti danno la direzione.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime

PROJECTS_FILE = "projects.json"
MAX_ACTIVE_PROJECTS = 7
MAX_LOG_ENTRIES = 60

STATUSES = ("active", "paused", "done", "dropped")


@dataclass
class Milestone:
    title: str
    done: bool = False
    done_at: str = ""


@dataclass
class Project:
    id: str
    title: str
    goal: str
    why: str = ""
    status: str = "active"
    created: str = ""
    updated: str = ""
    target_date: str = ""        # ISO date opzionale
    next_step: str = ""          # SEMPRE un'azione concreta, mai vaga
    milestones: list = field(default_factory=list)   # list[Milestone-dict]
    log: list = field(default_factory=list)          # [{ts, note}]

    def touch(self):
        self.updated = datetime.now().isoformat(timespec="seconds")

    def days_idle(self) -> int:
        try:
            last = datetime.fromisoformat(self.updated or self.created)
            return max(0, (datetime.now() - last).days)
        except Exception:
            return 0

    def progress(self) -> tuple[int, int]:
        done = sum(1 for m in self.milestones if m.get("done"))
        return done, len(self.milestones)


class ProjectError(Exception):
    pass


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _clean(text: str, limit: int) -> str:
    return " ".join((text or "").split())[:limit]


class Projects:
    def __init__(self, memory_dir: str):
        self.memory_dir = memory_dir
        self.path = os.path.join(memory_dir, PROJECTS_FILE)
        self._projects: list[Project] = []
        self._mtime: float = -1.0
        self._load()

    # ── Persistenza ──

    def _load(self):
        try:
            stat = os.stat(self.path)
        except OSError:
            self._projects = []
            self._mtime = -1.0
            return
        if stat.st_mtime == self._mtime:
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                self._projects = [Project(**p) for p in json.load(f)]
            self._mtime = stat.st_mtime
        except Exception:
            self._projects = []

    def _save(self):
        os.makedirs(self.memory_dir, exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump([asdict(p) for p in self._projects], f,
                      indent=2, ensure_ascii=False)
        try:
            self._mtime = os.stat(self.path).st_mtime
        except OSError:
            pass

    def _growth(self, note: str):
        try:
            from core.growth import record_growth_event
            record_growth_event(self.memory_dir, "projects", note)
        except Exception:
            pass

    # ── Query ──

    def active(self) -> list[Project]:
        self._load()
        return [p for p in self._projects if p.status == "active"]

    def get(self, project_id: str) -> Project:
        self._load()
        for p in self._projects:
            if p.id == project_id:
                return p
        raise ProjectError(f"Progetto non trovato: {project_id}")

    # ── Mutazioni ──

    def create(self, title: str, goal: str, why: str = "",
               next_step: str = "", target_date: str = "") -> Project:
        self._load()
        title = _clean(title, 120)
        goal = _clean(goal, 400)
        if len(title) < 4:
            raise ProjectError("Titolo troppo corto.")
        if len(goal) < 10:
            raise ProjectError(
                "L'obiettivo va descritto: come si capisce che il progetto è finito?"
            )
        if len(self.active()) >= MAX_ACTIVE_PROJECTS:
            raise ProjectError(
                f"Troppi progetti attivi ({MAX_ACTIVE_PROJECTS}): completa, "
                "metti in pausa o lascia cadere qualcosa prima."
            )
        pid = hashlib.sha1(title.lower().encode()).hexdigest()[:8]
        for p in self._projects:
            if p.id == pid and p.status in ("active", "paused"):
                raise ProjectError("Esiste già un progetto con questo titolo.")
        project = Project(
            id=pid, title=title, goal=goal,
            why=_clean(why, 300),
            next_step=_clean(next_step, 300),
            target_date=_clean(target_date, 10),
            created=_now(),
        )
        project.touch()
        self._projects.append(project)
        self._save()
        self._growth(f"progetto avviato: {title[:80]}")
        return project

    def note(self, project_id: str, note: str, next_step: str = "") -> Project:
        """Registra un avanzamento; opzionalmente aggiorna il prossimo passo."""
        p = self.get(project_id)
        if p.status != "active":
            raise ProjectError(f"Il progetto è {p.status}: riattivalo prima (resume).")
        note = _clean(note, 400)
        if len(note) < 5:
            raise ProjectError("La nota di avanzamento è vuota.")
        p.log.append({"ts": _now(), "note": note})
        if len(p.log) > MAX_LOG_ENTRIES:
            p.log = p.log[-MAX_LOG_ENTRIES:]
        if next_step:
            p.next_step = _clean(next_step, 300)
        p.touch()
        self._save()
        return p

    def set_next_step(self, project_id: str, next_step: str) -> Project:
        p = self.get(project_id)
        next_step = _clean(next_step, 300)
        if len(next_step) < 5:
            raise ProjectError("Il prossimo passo deve essere un'azione concreta.")
        p.next_step = next_step
        p.touch()
        self._save()
        return p

    def milestone_add(self, project_id: str, title: str) -> Project:
        p = self.get(project_id)
        title = _clean(title, 200)
        if len(title) < 4:
            raise ProjectError("Titolo milestone troppo corto.")
        p.milestones.append(asdict(Milestone(title=title)))
        p.touch()
        self._save()
        return p

    def milestone_done(self, project_id: str, milestone: str) -> Project:
        """Completa una milestone per indice (1-based) o per prefisso del titolo."""
        p = self.get(project_id)
        target = None
        if milestone.strip().isdigit():
            idx = int(milestone.strip()) - 1
            if 0 <= idx < len(p.milestones):
                target = p.milestones[idx]
        else:
            needle = milestone.strip().lower()
            for m in p.milestones:
                if m.get("title", "").lower().startswith(needle):
                    target = m
                    break
        if target is None:
            raise ProjectError(f"Milestone non trovata: {milestone}")
        if target.get("done"):
            raise ProjectError("Milestone già completata.")
        target["done"] = True
        target["done_at"] = _now()
        p.touch()
        self._save()
        self._growth(f"milestone: {target['title'][:60]} ({p.title[:40]})")
        return p

    def _set_status(self, project_id: str, status: str, note: str = "") -> Project:
        p = self.get(project_id)
        p.status = status
        if note:
            p.log.append({"ts": _now(), "note": _clean(note, 300)})
        p.touch()
        self._save()
        return p

    def pause(self, project_id: str, reason: str = "") -> Project:
        return self._set_status(project_id, "paused", note=f"in pausa: {reason}" if reason else "in pausa")

    def resume(self, project_id: str) -> Project:
        p = self.get(project_id)
        if len(self.active()) >= MAX_ACTIVE_PROJECTS:
            raise ProjectError(f"Troppi progetti attivi ({MAX_ACTIVE_PROJECTS}).")
        return self._set_status(project_id, "active", note="ripreso")

    def complete(self, project_id: str, outcome: str = "") -> Project:
        p = self._set_status(project_id, "done",
                             note=f"completato: {outcome}" if outcome else "completato")
        self._growth(f"progetto completato: {p.title[:80]}")
        return p

    def drop(self, project_id: str, reason: str = "") -> Project:
        return self._set_status(project_id, "dropped",
                                note=f"abbandonato: {reason}" if reason else "abbandonato")

    # ── Rendering ──

    def compile_prompt(self) -> str:
        """Blocco compatto per il system prompt: l'agente lavora SAPENDO
        quali progetti sono in corso, senza dover andare a leggerli."""
        active = self.active()
        if not active:
            return ""
        lines = ["## PROGETTI IN CORSO",
                 "Obiettivi a lungo termine concordati con l'owner. Se la "
                 "conversazione tocca uno di questi, collegala al progetto e "
                 "aggiorna l'avanzamento con il tool `project` (action=note)."]
        for p in active:
            done, total = p.progress()
            bits = [f"- [{p.id}] **{p.title}** — {p.goal}"]
            if total:
                bits.append(f"  Milestone: {done}/{total}")
            if p.next_step:
                bits.append(f"  Prossimo passo: {p.next_step}")
            if p.target_date:
                bits.append(f"  Scadenza: {p.target_date}")
            lines.append("\n".join(bits))
        return "\n".join(lines)

    def heartbeat_state(self) -> str:
        """Righe per lo stato vivo dell'heartbeat (progetti fermi in testa)."""
        active = sorted(self.active(), key=lambda p: -p.days_idle())
        if not active:
            return ""
        lines = [f"Progetti attivi ({len(active)}):"]
        for p in active[:4]:
            idle = p.days_idle()
            idle_txt = f" — fermo da {idle}g" if idle >= 2 else ""
            step = f" | prossimo passo: {p.next_step}" if p.next_step else " | SENZA prossimo passo (definiscine uno)"
            lines.append(f"- [{p.id}] {p.title}{idle_txt}{step}")
        return "\n".join(lines)

    def render_status(self) -> str:
        self._load()
        active = self.active()
        paused = [p for p in self._projects if p.status == "paused"]
        done = [p for p in self._projects if p.status == "done"]
        if not self._projects:
            return (
                "No projects yet. When you agree with the agent on a "
                "goal that takes weeks, it records it here and "
                "advances it one step at a time — even in autonomous cycles."
            )
        lines = [f"{len(active)} active · {len(paused)} paused · {len(done)} done", ""]
        for p in active:
            done_m, total_m = p.progress()
            lines.append(f"[{p.id}] {p.title}")
            lines.append(f"    goal: {p.goal}")
            if total_m:
                bar = "".join("#" if m.get("done") else "." for m in p.milestones)
                lines.append(f"    milestone {done_m}/{total_m}  {bar}")
            if p.next_step:
                lines.append(f"    next step: {p.next_step}")
            idle = p.days_idle()
            if idle >= 2:
                lines.append(f"    idle for {idle} days")
            if p.log:
                last = p.log[-1]
                lines.append(f"    last progress: {last['note'][:100]}")
            lines.append("")
        for p in paused:
            lines.append(f"[{p.id}] {p.title} (paused)")
        for p in done[-3:]:
            lines.append(f"[{p.id}] {p.title} ✓")
        return "\n".join(lines).rstrip()
