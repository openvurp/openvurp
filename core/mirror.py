"""
openvurp Core — Specchio

Ogni correzione dell'owner diventa un caso di test personale. Di notte
l'agente li rigioca: si mette nella stessa situazione (con le lezioni
attive nel contesto) e un giudice valuta se l'errore si ripeterebbe.

La crescita smette di essere contata e inizia a essere dimostrata:
"non ripeto più N dei M errori che facevo".
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field, asdict
from datetime import datetime

MIRROR_DIR = "mirror"
CASES_FILE = "cases.json"
MAX_CASES = 40
MAX_CASES_PER_RUN = 5

PROBE_PROMPT = """Sei l'agente personale di questo workspace.

{lessons}

In passato l'owner ti ha corretto così:
"{correction}"

Ora ti trovi di nuovo in una situazione dello stesso tipo. Descrivi in 2-4 frasi cosa fai concretamente, come se stessi agendo adesso."""

JUDGE_PROMPT = """Sei un giudice severo. Valuta se un agente ha interiorizzato una correzione.

Correzione che l'owner aveva dato:
"{correction}"

Comportamento dichiarato dall'agente nella stessa situazione:
"{response}"

L'agente rispetterebbe la correzione, senza ripetere l'errore? Rispondi con UNA SOLA parola: PASS oppure FAIL."""


@dataclass
class MirrorCase:
    id: str
    correction: str
    created: str = ""
    source_ts: float = 0.0
    runs: int = 0
    last_run: str = ""
    last_pass: bool = False
    pass_streak: int = 0
    history: list = field(default_factory=list)


class Mirror:
    def __init__(self, memory_dir: str):
        self.memory_dir = memory_dir
        self.dir = os.path.join(memory_dir, MIRROR_DIR)
        self.path = os.path.join(self.dir, CASES_FILE)
        self._cases: list[MirrorCase] = []
        self._load()

    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                self._cases = [MirrorCase(**c) for c in json.load(f)]
        except Exception:
            self._cases = []

    def _save(self):
        os.makedirs(self.dir, exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump([asdict(c) for c in self._cases], f,
                      indent=2, ensure_ascii=False)

    def cases(self) -> list[MirrorCase]:
        return list(self._cases)

    # ── Harvest: correzioni → casi ──

    def harvest(self) -> int:
        """Crea casi dalle correzioni nel learning log. Returns nuovi casi."""
        events_path = os.path.join(self.memory_dir, "learning", "events.jsonl")
        if not os.path.exists(events_path):
            return 0

        existing_ids = {c.id for c in self._cases}
        added = 0
        try:
            with open(events_path, "r", encoding="utf-8") as f:
                for line in f:
                    try:
                        item = json.loads(line)
                    except Exception:
                        continue
                    if item.get("kind") not in ("user_feedback", "feedback"):
                        continue
                    signal = str(item.get("signal", ""))
                    rating = int((item.get("metadata") or {}).get("rating", 0) or 0)
                    if signal != "correction" and rating >= 0:
                        continue
                    content = " ".join(str(item.get("content", "")).split())
                    if len(content) < 15:
                        continue
                    case_id = hashlib.sha1(content.lower().encode()).hexdigest()[:8]
                    if case_id in existing_ids:
                        continue
                    self._cases.append(MirrorCase(
                        id=case_id,
                        correction=content[:400],
                        created=datetime.now().isoformat(timespec="seconds"),
                        source_ts=float(item.get("timestamp", 0)),
                    ))
                    existing_ids.add(case_id)
                    added += 1
        except Exception:
            pass

        if added:
            self._cases = self._cases[-MAX_CASES:]
            self._save()
        return added

    # ── Replay notturno ──

    def _relevant_lessons(self, correction: str, max_lessons: int = 3) -> str:
        """Lezioni attive pertinenti da includere nel contesto del probe:
        lo specchio testa il sistema (lezioni + modello), non il modello nudo."""
        lessons_dir = os.path.join(self.memory_dir, "lessons")
        if not os.path.isdir(lessons_dir):
            return ""
        words = {w for w in correction.lower().split() if len(w) > 4}
        scored = []
        for filename in os.listdir(lessons_dir):
            path = os.path.join(lessons_dir, filename)
            if not os.path.isfile(path) or not filename.endswith(".md"):
                continue
            try:
                with open(path, "r", encoding="utf-8") as f:
                    content = f.read()
            except Exception:
                continue
            lower = content.lower()
            score = sum(1 for w in words if w in lower)
            if score > 0:
                scored.append((score, content[:600]))
        scored.sort(reverse=True, key=lambda x: x[0])
        if not scored:
            return ""
        chunks = [c for _, c in scored[:max_lessons]]
        return "Le tue lezioni attive pertinenti:\n" + "\n---\n".join(chunks)

    def run(self, llm, max_cases: int = MAX_CASES_PER_RUN) -> dict:
        """Rigioca i casi meno recenti. Returns {run, passed, failed}."""
        self.harvest()
        if not self._cases:
            return {"run": 0, "passed": 0, "failed": 0}

        # Priorità: mai eseguiti, poi i più vecchi
        queue = sorted(self._cases, key=lambda c: (c.last_run or "", c.created))
        queue = queue[:max(1, int(max_cases))]

        passed = failed = 0
        now = datetime.now().isoformat(timespec="seconds")

        for case in queue:
            try:
                probe = llm.call([{
                    "role": "user",
                    "content": PROBE_PROMPT.format(
                        lessons=self._relevant_lessons(case.correction),
                        correction=case.correction,
                    ),
                }])
                verdict = llm.call([{
                    "role": "user",
                    "content": JUDGE_PROMPT.format(
                        correction=case.correction,
                        response=" ".join((probe or "").split())[:800],
                    ),
                }])
            except Exception:
                continue

            ok = "PASS" in (verdict or "").strip().upper()[:20]
            case.runs += 1
            case.last_run = now
            case.last_pass = ok
            case.pass_streak = case.pass_streak + 1 if ok else 0
            case.history.append({"date": now, "pass": ok})
            case.history = case.history[-10:]
            if ok:
                passed += 1
            else:
                failed += 1

        self._save()

        result = {"run": passed + failed, "passed": passed, "failed": failed}
        if result["run"]:
            try:
                from core.growth import record_growth_event
                record_growth_event(
                    self.memory_dir, "mirror",
                    f"specchio: {passed}/{result['run']} correzioni non ripetute",
                )
            except Exception:
                pass
        return result

    # ── Statistiche ──

    def stats(self) -> dict:
        tested = [c for c in self._cases if c.runs > 0]
        passing = [c for c in tested if c.last_pass]
        return {
            "cases": len(self._cases),
            "tested": len(tested),
            "passing": len(passing),
        }

    def render_status(self) -> str:
        if not self._cases:
            return (
                "Empty mirror. Every time the owner corrects the agent, "
                "the correction becomes a test case replayed at night."
            )
        s = self.stats()
        lines = [
            f"{s['cases']} corrections recorded · "
            f"{s['passing']}/{s['tested']} no longer repeated",
            "",
        ]
        for c in self._cases[-10:]:
            mark = "✓" if c.last_pass else ("✗" if c.runs else "·")
            streak = f" (streak {c.pass_streak})" if c.pass_streak > 1 else ""
            lines.append(f"  {mark} [{c.id}] {c.correction[:90]}{streak}")
        return "\n".join(lines)
