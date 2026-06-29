"""
openvurp Core — Curiosità

L'agente tiene una lista di domande aperte: cose che ha notato di non
sapere sul mondo dell'owner. Nei cicli heartbeat senza lavoro urgente
ne sceglie UNA, studia con budget (web, lettura) e archivia la risposta.

"Quando non ho niente da fare, studio."
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime

CURIOSITY_FILE = "curiosity.json"
MAX_OPEN_QUESTIONS = 20


@dataclass
class Question:
    id: str
    question: str
    why: str = ""
    status: str = "open"     # open | answered | dropped
    created: str = ""
    answered: str = ""
    answer_summary: str = ""


class CuriosityError(Exception):
    pass


class Curiosity:
    def __init__(self, memory_dir: str):
        self.memory_dir = memory_dir
        self.path = os.path.join(memory_dir, CURIOSITY_FILE)
        self._questions: list[Question] = []
        self._mtime: float = -1.0
        self._load()

    def _load(self):
        try:
            stat = os.stat(self.path)
        except OSError:
            self._questions = []
            self._mtime = -1.0
            return
        if stat.st_mtime == self._mtime:
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                self._questions = [Question(**q) for q in json.load(f)]
            self._mtime = stat.st_mtime
        except Exception:
            self._questions = []

    def _save(self):
        os.makedirs(self.memory_dir, exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump([asdict(q) for q in self._questions], f,
                      indent=2, ensure_ascii=False)
        try:
            self._mtime = os.stat(self.path).st_mtime
        except OSError:
            pass

    def open_questions(self) -> list[Question]:
        self._load()
        return [q for q in self._questions if q.status == "open"]

    def add(self, question: str, why: str = "") -> Question:
        self._load()
        clean = " ".join((question or "").split())
        if len(clean) < 10:
            raise CuriosityError("Domanda troppo corta per valere uno studio.")
        if len(self.open_questions()) >= MAX_OPEN_QUESTIONS:
            raise CuriosityError(
                f"Troppe domande aperte ({MAX_OPEN_QUESTIONS}): "
                "rispondi o lascia cadere qualcosa prima di aggiungerne altre."
            )
        qid = hashlib.sha1(clean.lower().encode()).hexdigest()[:8]
        for q in self._questions:
            if q.id == qid and q.status == "open":
                raise CuriosityError("Questa domanda è già in lista.")
        q = Question(
            id=qid,
            question=clean[:300],
            why=" ".join((why or "").split())[:200],
            created=datetime.now().isoformat(timespec="seconds"),
        )
        self._questions.append(q)
        self._save()
        return q

    def answer(self, question_id: str, summary: str) -> Question:
        self._load()
        for q in self._questions:
            if q.id == question_id and q.status == "open":
                q.status = "answered"
                q.answered = datetime.now().isoformat(timespec="seconds")
                q.answer_summary = " ".join((summary or "").split())[:500]
                self._save()
                try:
                    from core.growth import record_growth_event
                    record_growth_event(
                        self.memory_dir, "curiosity",
                        f"studiato: {q.question[:80]}",
                    )
                except Exception:
                    pass
                return q
        raise CuriosityError(f"Domanda aperta non trovata: {question_id}")

    def drop(self, question_id: str) -> Question:
        self._load()
        for q in self._questions:
            if q.id == question_id and q.status == "open":
                q.status = "dropped"
                self._save()
                return q
        raise CuriosityError(f"Domanda aperta non trovata: {question_id}")

    def render_status(self) -> str:
        self._load()
        open_qs = self.open_questions()
        answered = [q for q in self._questions if q.status == "answered"]
        if not open_qs and not answered:
            return (
                "No curiosities yet. When the agent notices something it "
                "doesn't know about your world, it lists it and studies it in "
                "idle moments."
            )
        lines = [f"{len(open_qs)} open questions · {len(answered)} studied", ""]
        for q in open_qs:
            lines.append(f"[{q.id}] {q.question}")
            if q.why:
                lines.append(f"         why: {q.why}")
        if answered:
            lines.append("")
            lines.append("Recently studied:")
            for q in answered[-3:]:
                lines.append(f"  {q.question[:80]}")
                if q.answer_summary:
                    lines.append(f"    → {q.answer_summary[:120]}")
        return "\n".join(lines)
