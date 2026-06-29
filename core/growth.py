"""
openvurp Core — Growth Diary

L'agente nasce (BOOTSTRAP), impara e cresce. Questo modulo rende la crescita
visibile e misurabile: atto di nascita, journal degli eventi di crescita,
report leggibile (/growth).

Eventi tipici: born, lesson_promoted, lesson_rolled_back, dreaming,
memory_indexed, skill_added, identity_updated.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta

BIRTH_FILE = "birth.json"
GROWTH_FILE = "growth.jsonl"


def ensure_birth(memory_dir: str) -> datetime:
    """Garantisce l'atto di nascita. Dopo un reset il file sparisce:
    la prima esecuzione successiva è una nuova nascita."""
    os.makedirs(memory_dir, exist_ok=True)
    path = os.path.join(memory_dir, BIRTH_FILE)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return datetime.fromisoformat(data["born_at"])
        except Exception:
            pass

    born = datetime.now()
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"born_at": born.isoformat(timespec="seconds")}, f, indent=2)
        record_growth_event(memory_dir, "born", "Prima esecuzione: l'agente è nato.")
    except Exception:
        pass
    return born


def record_growth_event(memory_dir: str, kind: str, detail: str,
                        meta: dict | None = None) -> None:
    """Registra un evento di crescita nel journal."""
    try:
        path = os.path.join(memory_dir, GROWTH_FILE)
        os.makedirs(memory_dir, exist_ok=True)
        item = {
            "ts": time.time(),
            "date": datetime.now().isoformat(timespec="seconds"),
            "kind": (kind or "event").strip()[:40],
            "detail": " ".join((detail or "").split())[:400],
        }
        if meta:
            item["meta"] = meta
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")
    except Exception:
        pass


def read_growth_events(memory_dir: str, since: float = 0.0,
                       max_events: int = 500) -> list[dict]:
    path = os.path.join(memory_dir, GROWTH_FILE)
    if not os.path.exists(path):
        return []
    events: list[dict] = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except Exception:
                    continue
                if item.get("ts", 0) >= since:
                    events.append(item)
    except Exception:
        pass
    return events[-max_events:]


@dataclass
class GrowthReport:
    born_at: str = ""
    age_days: int = 0
    window_days: int = 7
    lessons_total: int = 0
    lessons_new: int = 0
    promotions: int = 0
    rollbacks: int = 0
    dreaming_runs: int = 0
    turns_completed: int = 0
    learning_events: dict = field(default_factory=dict)
    open_loops_open: int = 0
    open_loops_closed: int = 0
    semantic_memories: int = 0
    semantic_with_embeddings: int = 0
    mirror_cases: int = 0
    mirror_tested: int = 0
    mirror_passing: int = 0
    diary_entries: int = 0
    dream_insights: int = 0
    recent_events: list = field(default_factory=list)

    def render(self) -> str:
        lines = [
            f"Born on {self.born_at} — {self.age_days} days alive.",
            "",
            f"Last {self.window_days} days:",
            f"  turns completed           {self.turns_completed}",
            f"  new lessons               {self.lessons_new}  (total: {self.lessons_total})",
            f"  verified promotions       {self.promotions}"
            + (f"  ·  rollback {self.rollbacks}" if self.rollbacks else ""),
            f"  dreams (consolidations)   {self.dreaming_runs}",
            f"  open loops                {self.open_loops_open} aperti · {self.open_loops_closed} chiusi",
            f"  semantic memory           {self.semantic_memories} memories"
            + (f" ({self.semantic_with_embeddings} with embeddings)"
               if self.semantic_with_embeddings else ""),
        ]
        if self.mirror_cases:
            lines.append(
                f"  specchio                  {self.mirror_passing}/{self.mirror_tested} "
                f"corrections no longer repeated (of {self.mirror_cases} recorded)"
            )
        if self.diary_entries:
            lines.append(f"  diary entries             {self.diary_entries}")
        if self.dream_insights:
            lines.append(f"  dream insights            {self.dream_insights}")
        if self.learning_events:
            parts = ", ".join(f"{k}: {v}" for k, v in sorted(self.learning_events.items()))
            lines.append(f"  learning signals          {parts}")
        if self.recent_events:
            lines.append("")
            lines.append("Recent growth:")
            for evt in self.recent_events[-8:]:
                date = str(evt.get("date", ""))[:16].replace("T", " ")
                lines.append(f"  {date}  {evt.get('kind', '?')}: {evt.get('detail', '')[:90]}")
        if self.age_days == 0 and not self.recent_events:
            lines.append("")
            lines.append("Just born. Everything I learn will start here.")
        return "\n".join(lines)


def build_growth_report(memory_dir: str, days: int = 7,
                        memory_manager=None) -> GrowthReport:
    """Costruisce il report di crescita dal journal e dallo stato su disco."""
    report = GrowthReport(window_days=max(1, int(days)))
    now = datetime.now()
    since_ts = (now - timedelta(days=report.window_days)).timestamp()

    # Nascita
    born = ensure_birth(memory_dir)
    report.born_at = born.strftime("%Y-%m-%d %H:%M")
    report.age_days = max(0, (now - born).days)

    # Lezioni
    lessons_dir = os.path.join(memory_dir, "lessons")
    if os.path.isdir(lessons_dir):
        for f in os.listdir(lessons_dir):
            fp = os.path.join(lessons_dir, f)
            if not os.path.isfile(fp):
                continue
            report.lessons_total += 1
            if os.path.getmtime(fp) >= since_ts:
                report.lessons_new += 1

    # Eventi di crescita
    events = read_growth_events(memory_dir)
    window_events = [e for e in events if e.get("ts", 0) >= since_ts]
    report.recent_events = window_events
    for evt in window_events:
        kind = evt.get("kind", "")
        if kind == "lesson_promoted":
            report.promotions += 1
        elif kind == "lesson_rolled_back":
            report.rollbacks += 1
        elif kind == "dreaming":
            report.dreaming_runs += 1
        elif kind == "diary":
            report.diary_entries += 1
        elif kind == "dream_insight":
            report.dream_insights += 1

    # Specchio: correzioni rigiocate
    try:
        from core.mirror import Mirror
        stats = Mirror(memory_dir).stats()
        report.mirror_cases = stats["cases"]
        report.mirror_tested = stats["tested"]
        report.mirror_passing = stats["passing"]
    except Exception:
        pass

    # Eventi di learning (segnali)
    events_path = os.path.join(memory_dir, "learning", "events.jsonl")
    if os.path.exists(events_path):
        try:
            with open(events_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        item = json.loads(line)
                    except Exception:
                        continue
                    if item.get("timestamp", 0) >= since_ts:
                        kind = item.get("kind", "event")
                        report.learning_events[kind] = report.learning_events.get(kind, 0) + 1
        except Exception:
            pass

    # Turni completati dal task journal
    journal_dir = os.path.join(memory_dir, "task_journal")
    if os.path.isdir(journal_dir):
        cutoff_day = (now - timedelta(days=report.window_days)).date()
        for f in os.listdir(journal_dir):
            match = re.fullmatch(r"(\d{4}-\d{2}-\d{2})\.jsonl", f)
            if not match:
                continue
            try:
                day = datetime.fromisoformat(match.group(1)).date()
            except Exception:
                continue
            if day < cutoff_day:
                continue
            try:
                with open(os.path.join(journal_dir, f), "r", encoding="utf-8") as fh:
                    for line in fh:
                        if '"type": "turn_finish"' in line and '"status": "completed"' in line:
                            report.turns_completed += 1
            except Exception:
                continue

    # Open loops
    loops_path = os.path.join(memory_dir, "open_loops.json")
    if os.path.exists(loops_path):
        try:
            with open(loops_path, "r", encoding="utf-8") as f:
                loops = json.load(f)
            if isinstance(loops, list):
                for loop in loops:
                    status = str(loop.get("status", "open")) if isinstance(loop, dict) else "open"
                    if status == "open":
                        report.open_loops_open += 1
                    else:
                        report.open_loops_closed += 1
        except Exception:
            pass

    # Memoria semantica
    vector = getattr(memory_manager, "vector", None) if memory_manager else None
    if vector is not None:
        try:
            stats = vector.stats()
            report.semantic_memories = int(stats.get("total_memories", 0))
            report.semantic_with_embeddings = int(stats.get("with_embeddings", 0))
        except Exception:
            pass

    return report
