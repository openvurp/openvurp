"""
openvurp Core - Verified Learning Loop

Raccoglie segnali locali di apprendimento senza addestrare modelli e senza
inviare dati fuori macchina. Gli eventi restano candidati finche non vengono
promossi in una lezione stabile.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from core.security.audit import redact
from core.tools import ToolResult


@dataclass
class LearningEvent:
    timestamp: float
    kind: str
    topic: str
    content: str
    signal: str = ""
    actor: str = "agent"
    source: str = "cli"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class LearningCandidate:
    id: str
    kind: str
    title: str
    recommendation: str
    evidence_count: int
    confidence: float
    tags: list[str] = field(default_factory=list)


@dataclass
class LearningReviewReport:
    events_considered: int
    candidates: list[LearningCandidate]
    candidates_path: str

    def render(self) -> str:
        if not self.candidates:
            return (
                f"Learning review: {self.events_considered} eventi considerati, "
                "nessun candidato abbastanza forte."
            )

        lines = [
            f"Learning review: {self.events_considered} eventi considerati, "
            f"{len(self.candidates)} candidati.",
        ]
        for candidate in self.candidates:
            lines.append(
                f"- [{candidate.id}] {candidate.title} "
                f"(confidenza {candidate.confidence:.2f}, evidenze {candidate.evidence_count})"
            )
            lines.append(f"  {candidate.recommendation}")
        lines.append(f"Candidati salvati in {self.candidates_path}.")
        return "\n".join(lines)


class LearningLoop:
    EVENTS_FILE = "events.jsonl"
    CANDIDATES_FILE = "candidates.json"

    def __init__(self, memory_dir: str):
        self.memory_dir = memory_dir
        self.learning_dir = os.path.join(memory_dir, "learning")
        self.lessons_dir = os.path.join(memory_dir, "lessons")
        os.makedirs(self.learning_dir, exist_ok=True)
        os.makedirs(self.lessons_dir, exist_ok=True)
        os.makedirs(self.memory_dir, exist_ok=True)

    @property
    def events_path(self) -> str:
        return os.path.join(self.learning_dir, self.EVENTS_FILE)

    @property
    def candidates_path(self) -> str:
        return os.path.join(self.learning_dir, self.CANDIDATES_FILE)

    def detect_user_signal(self, text: str) -> dict[str, str] | None:
        normalized = " ".join((text or "").strip().split())
        if not normalized:
            return None

        lower = normalized.lower()
        markers = (
            ("explicit_memory", ("ricorda", "memorizza", "salva questa", "remember this")),
            ("correction", ("hai sbagliato", "sbagliato", "non fare", "non devi")),
            ("preference", ("la prossima volta", "preferisco che", "vorrei che")),
        )
        import re as _re
        for signal, options in markers:
            for marker in options:
                # Parola intera: "ricordando perché contava" NON è un
                # "ricorda questo" — il substring match qui inquinava la
                # memoria con testo che parlava soltanto di ricordare.
                if _re.search(rf"\b{_re.escape(marker)}\b", lower):
                    return {
                        "signal": signal,
                        "marker": marker,
                        "topic": self._topic_from_text(normalized, marker),
                        "content": normalized,
                    }
        return None

    def record_user_signal(self, text: str, actor: str = "user",
                           source: str = "cli") -> LearningEvent | None:
        detected = self.detect_user_signal(text)
        if not detected:
            return None

        event = self.record_event(
            kind="user_feedback",
            topic=detected["topic"],
            content=detected["content"],
            signal=detected["signal"],
            actor=actor,
            source=source,
            metadata={"marker": detected["marker"]},
        )
        self._append_daily_note(event)
        return event

    def record_feedback(self, topic: str, feedback: str, rating: int = 0,
                        desired_change: str = "", tags: list[str] | None = None,
                        actor: str = "user", source: str = "cli") -> LearningEvent:
        topic = topic.strip() or "feedback"
        feedback = feedback.strip()
        desired_change = desired_change.strip()
        content = feedback
        if desired_change:
            content = f"{feedback}\nDesired change: {desired_change}"

        event = self.record_event(
            kind="feedback",
            topic=topic,
            content=content,
            signal="rating",
            actor=actor,
            source=source,
            metadata={
                "rating": max(-1, min(1, int(rating or 0))),
                "tags": tags or [],
            },
        )
        self._append_daily_note(event)
        return event

    def record_tool_failure(self, tool_name: str, args: dict,
                            result: ToolResult, actor: str = "agent",
                            source: str = "cli") -> LearningEvent | None:
        if result.success:
            return None

        args_keys = sorted(str(key) for key in (args or {}).keys())
        error = result.error or result.output or "tool failed"
        return self.record_event(
            kind="tool_failure",
            topic=tool_name or "unknown_tool",
            content=str(error),
            signal=result.error_type.value,
            actor=actor,
            source=source,
            metadata={
                "args_keys": args_keys,
                "duration_ms": result.duration_ms,
                "tool_name": result.tool_name or tool_name,
            },
        )

    def record_event(self, kind: str, topic: str, content: str,
                     signal: str = "", actor: str = "agent",
                     source: str = "cli",
                     metadata: dict[str, Any] | None = None) -> LearningEvent:
        event = LearningEvent(
            timestamp=time.time(),
            kind=kind,
            topic=self._clean_topic(topic),
            content=self._clean_content(content),
            signal=(signal or "").strip()[:80],
            actor=(actor or "agent").strip()[:120],
            source=(source or "cli").strip()[:80],
            metadata=metadata or {},
        )
        self._append_jsonl(self.events_path, asdict(event))
        return event

    def read_events(self, max_events: int = 200) -> list[LearningEvent]:
        if not os.path.exists(self.events_path):
            return []

        events: list[LearningEvent] = []
        try:
            with open(self.events_path, "r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    raw = json.loads(line)
                    events.append(LearningEvent(**raw))
        except Exception:
            return events[-max_events:]
        return events[-max_events:]

    def review(self, max_events: int = 200,
               min_repeats: int = 2) -> LearningReviewReport:
        events = self.read_events(max_events=max_events)
        candidates = self._build_candidates(events, min_repeats=max(1, int(min_repeats)))

        with open(self.candidates_path, "w", encoding="utf-8") as handle:
            json.dump([asdict(candidate) for candidate in candidates],
                      handle, indent=2, ensure_ascii=False)

        return LearningReviewReport(
            events_considered=len(events),
            candidates=candidates,
            candidates_path=self.candidates_path,
        )

    # ── Verifica pre-promozione ──

    MIN_PROMOTE_CONFIDENCE = 0.6
    MIN_PROMOTE_EVIDENCE = 2

    def verify_candidate(self, candidate: LearningCandidate | None,
                         topic: str, content: str) -> tuple[bool, list[str]]:
        """Verifica un candidato prima della promozione.

        La promessa del progetto è "verified self-improvement": niente
        diventa lezione stabile senza passare questi controlli.
        Returns (ok, lista problemi).
        """
        problems: list[str] = []

        if not topic.strip():
            problems.append("topic vuoto")
        if len(content.strip()) < 20:
            problems.append("contenuto troppo corto per essere una lezione utile")
        if len(content) > 4000:
            problems.append("contenuto troppo lungo: distilla prima di promuovere")

        # Mai promuovere materiale con segreti
        if redact(content) != content or redact(topic) != topic:
            problems.append("contiene materiale che sembra un segreto/credenziale")

        if candidate is not None:
            if candidate.confidence < self.MIN_PROMOTE_CONFIDENCE:
                problems.append(
                    f"confidenza {candidate.confidence:.2f} sotto soglia "
                    f"{self.MIN_PROMOTE_CONFIDENCE} — servono più evidenze"
                )
            if candidate.evidence_count < self.MIN_PROMOTE_EVIDENCE:
                problems.append(
                    f"solo {candidate.evidence_count} evidenza/e: "
                    f"minimo {self.MIN_PROMOTE_EVIDENCE}"
                )

        # Non duplicare lezioni già attive
        slug = re.sub(r"[^a-z0-9]+", "-", topic.lower()).strip("-")[:50]
        if slug:
            for existing in os.listdir(self.lessons_dir):
                if slug in existing and os.path.isfile(
                        os.path.join(self.lessons_dir, existing)):
                    problems.append(f"esiste già una lezione simile: {existing}")
                    break

        return (not problems, problems)

    def promote_candidate(self, candidate_id: str = "", topic: str = "",
                          content: str = "", tags: list[str] | None = None,
                          actor: str = "agent", source: str = "cli",
                          force: bool = False) -> str:
        candidate = None
        if candidate_id:
            candidate = self._load_candidate(candidate_id)
            if not candidate:
                return f"[ERRORE] Candidate learning non trovato: {candidate_id}"

        if candidate:
            topic = topic.strip() or candidate.title
            content = content.strip() or candidate.recommendation
            tags = tags or candidate.tags
        else:
            topic = topic.strip()
            content = content.strip()

        if not topic or not content:
            return "[ERRORE] Servono candidate_id oppure topic e content."

        # Verifica prima di promuovere
        ok, problems = self.verify_candidate(candidate, topic, content)
        if not ok and not force:
            return (
                "[VERIFICA FALLITA] La promozione è stata bloccata:\n- "
                + "\n- ".join(problems)
                + "\nUsa force=true solo se sei sicuro e l'owner è d'accordo."
            )

        path = self._write_lesson(
            topic, content, tags or [],
            provenance={
                "candidate_id": candidate_id or "manual",
                "evidence": candidate.evidence_count if candidate else 1,
                "confidence": candidate.confidence if candidate else 0.0,
                "verified": ok,
                "forced": bool(force and not ok),
                "actor": actor,
                "source": source,
            },
        )
        self.record_event(
            kind="promotion",
            topic=topic,
            content=f"Promossa lezione: {os.path.basename(path)}",
            signal="lesson",
            actor=actor,
            source=source,
            metadata={"candidate_id": candidate_id, "verified": ok},
        )
        try:
            from core.growth import record_growth_event
            record_growth_event(
                self.memory_dir, "lesson_promoted",
                f"{topic} ({'verificata' if ok else 'forzata'})",
                meta={"file": os.path.basename(path)},
            )
        except Exception:
            pass
        verdict = "verificata e promossa" if ok else "promossa con force (verifica fallita)"
        return f"[OK] Lezione {verdict} in {path}"

    def rollback_lesson(self, filename: str, reason: str = "",
                        actor: str = "agent", source: str = "cli") -> str:
        """Ritira una lezione promossa (rollback). Il file finisce in
        lessons/.retired/ con il motivo, così la storia resta ispezionabile."""
        filename = os.path.basename((filename or "").strip())
        if not filename:
            return "[ERRORE] Serve il nome file della lezione."
        path = os.path.join(self.lessons_dir, filename)
        if not os.path.isfile(path):
            return f"[ERRORE] Lezione non trovata: {filename}"

        retired_dir = os.path.join(self.lessons_dir, ".retired")
        os.makedirs(retired_dir, exist_ok=True)
        target = os.path.join(retired_dir, filename)
        try:
            with open(path, "r", encoding="utf-8") as f:
                body = f.read()
            stamp = datetime.now().isoformat(timespec="seconds")
            body += f"\n\n---\nritirata: {stamp}\nmotivo: {reason or 'non specificato'}\n"
            with open(target, "w", encoding="utf-8") as f:
                f.write(body)
            os.remove(path)
        except Exception as e:
            return f"[ERRORE] Rollback fallito: {e}"

        self.record_event(
            kind="rollback",
            topic=filename,
            content=f"Lezione ritirata: {reason or 'non specificato'}",
            signal="lesson",
            actor=actor,
            source=source,
        )
        try:
            from core.growth import record_growth_event
            record_growth_event(self.memory_dir, "lesson_rolled_back",
                                f"{filename}: {reason or 'non specificato'}")
        except Exception:
            pass
        return f"[OK] Lezione ritirata in lessons/.retired/{filename}"

    def record_task_completion(self, goal: str, tools_used: list[str],
                               source: str = "cli") -> None:
        """Registra un task completato con successo: è la materia prima
        dei candidati 'procedure' (skill ricavate da lavoro reale)."""
        if not (goal or "").strip() or not tools_used:
            return
        try:
            self.record_event(
                kind="task_completed",
                topic=goal[:120],
                content=f"Tools: {', '.join(tools_used[:10])}",
                signal="success",
                source=source,
                metadata={"tools": tools_used[:10]},
            )
        except Exception:
            pass

    def _build_candidates(self, events: list[LearningEvent],
                          min_repeats: int) -> list[LearningCandidate]:
        candidates: list[LearningCandidate] = []
        feedback_groups: dict[str, list[LearningEvent]] = {}
        failure_groups: dict[str, list[LearningEvent]] = {}

        task_groups: dict[str, list[LearningEvent]] = {}

        for event in events:
            if event.kind in {"feedback", "user_feedback"}:
                key = self._fingerprint(
                    f"{event.kind}:{event.signal}:{event.topic}:{event.content[:220]}"
                )
                feedback_groups.setdefault(key, []).append(event)
            elif event.kind == "tool_failure":
                tool_name = str(event.metadata.get("tool_name") or event.topic)
                key = f"{tool_name}:{event.signal}"
                failure_groups.setdefault(key, []).append(event)
            elif event.kind == "task_completed":
                # Raggruppa per sequenza di tool: stessi tool su task
                # ripetuti = procedura riusabile candidata a skill
                tools = event.metadata.get("tools") or []
                if tools:
                    key = ",".join(str(t) for t in tools[:5])
                    task_groups.setdefault(key, []).append(event)

        for group in feedback_groups.values():
            latest = group[-1]
            rating_bonus = 0.0
            for event in group:
                rating_bonus += max(0, int(event.metadata.get("rating", 0))) * 0.05
            confidence = min(0.95, 0.55 + (0.12 * len(group)) + rating_bonus)
            title = f"Preferenza: {latest.topic}"
            recommendation = (
                "Integra questa preferenza nelle prossime risposte: "
                f"{latest.content[:240]}"
            )
            candidates.append(self._candidate(
                kind="preference",
                title=title,
                recommendation=recommendation,
                evidence_count=len(group),
                confidence=confidence,
                tags=["feedback", latest.signal or "user"],
            ))

        for key, group in failure_groups.items():
            if len(group) < min_repeats:
                continue
            latest = group[-1]
            tool_name, error_type = key.split(":", 1)
            candidates.append(self._candidate(
                kind="tool_reliability",
                title=f"Errore ricorrente: {tool_name}",
                recommendation=(
                    f"Indaga gli errori {error_type} ripetuti su {tool_name}. "
                    f"Ultimo errore: {latest.content[:180]}"
                ),
                evidence_count=len(group),
                confidence=min(0.9, 0.45 + 0.1 * len(group)),
                tags=["tool_failure", tool_name, error_type],
            ))

        for key, group in task_groups.items():
            if len(group) < max(min_repeats, 3):
                continue
            latest = group[-1]
            candidates.append(self._candidate(
                kind="procedure",
                title=f"Procedura ricorrente: {latest.topic}",
                recommendation=(
                    f"Hai completato {len(group)} task simili usando la sequenza "
                    f"di tool [{key}]. Valuta di distillare la procedura in una "
                    f"skill in skills/ così diventa riusabile e testabile. "
                    f"Ultimo esempio: {latest.topic[:120]}"
                ),
                evidence_count=len(group),
                confidence=min(0.9, 0.5 + 0.1 * len(group)),
                tags=["procedure", "task_trace"],
            ))

        candidates.sort(key=lambda item: (item.confidence, item.evidence_count), reverse=True)
        return candidates

    def _candidate(self, kind: str, title: str, recommendation: str,
                   evidence_count: int, confidence: float,
                   tags: list[str]) -> LearningCandidate:
        raw_id = f"{kind}:{title}:{recommendation}"
        return LearningCandidate(
            id=self._fingerprint(raw_id)[:12],
            kind=kind,
            title=title[:120],
            recommendation=recommendation[:500],
            evidence_count=evidence_count,
            confidence=round(float(confidence), 3),
            tags=[tag for tag in tags if tag][:8],
        )

    def _load_candidate(self, candidate_id: str) -> LearningCandidate | None:
        if not os.path.exists(self.candidates_path):
            return None
        try:
            with open(self.candidates_path, "r", encoding="utf-8") as handle:
                candidates = json.load(handle)
        except Exception:
            return None
        for raw in candidates:
            if raw.get("id") == candidate_id:
                return LearningCandidate(**raw)
        return None

    def _write_lesson(self, topic: str, content: str, tags: list[str],
                      provenance: dict | None = None) -> str:
        date = datetime.now().strftime("%Y-%m-%d")
        slug = re.sub(r"[^a-z0-9]+", "-", topic.lower()).strip("-")[:50] or "lesson"
        unique = self._fingerprint(f"{topic}:{content}")[:8]
        path = os.path.join(self.lessons_dir, f"{date}_{slug}_{unique}.md")

        header = [f"# {topic}", f"data: {date}", "versione: 1"]
        if tags:
            header.append("tags: " + ", ".join(tags[:8]))
        if provenance:
            header.append(
                "provenienza: candidato={cid}; evidenze={ev}; "
                "confidenza={conf:.2f}; verificata={ver}".format(
                    cid=provenance.get("candidate_id", "manual"),
                    ev=provenance.get("evidence", 1),
                    conf=float(provenance.get("confidence", 0.0)),
                    ver="sì" if provenance.get("verified") else
                        ("forzata" if provenance.get("forced") else "no"),
                )
            )
        body = "\n".join(header) + "\n\n" + self._clean_content(content) + "\n"

        with open(path, "w", encoding="utf-8") as handle:
            handle.write(body)
        return path

    def _append_daily_note(self, event: LearningEvent) -> None:
        date = datetime.now().strftime("%Y-%m-%d")
        path = os.path.join(self.memory_dir, f"{date}.md")
        stamp = datetime.fromtimestamp(event.timestamp).isoformat(timespec="seconds")
        line = (
            f"- {stamp} [{event.source}/{event.actor}] "
            f"{event.signal or event.kind}: {event.content}"
        )
        try:
            needs_header = not os.path.exists(path) or os.path.getsize(path) == 0
            with open(path, "a", encoding="utf-8") as handle:
                if needs_header:
                    handle.write(f"# {date}\n\n")
                handle.write(line[:1000] + "\n")
        except Exception:
            pass

    def _append_jsonl(self, path: str, item: dict[str, Any]) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n")

    def _topic_from_text(self, text: str, marker: str) -> str:
        lower = text.lower()
        start = lower.find(marker)
        if start >= 0:
            fragment = text[start + len(marker):].strip(" :,-")
        else:
            fragment = text
        return fragment[:80] or "user_feedback"

    def _clean_topic(self, topic: str) -> str:
        topic = " ".join((topic or "feedback").strip().split())
        return redact(topic)[:120] or "feedback"

    def _clean_content(self, content: str) -> str:
        content = " ".join((content or "").strip().split())
        return redact(content)[:1000]

    def _fingerprint(self, text: str) -> str:
        return hashlib.sha1(text.encode("utf-8")).hexdigest()
