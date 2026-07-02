"""
openvurp Core — Dreaming / Memory Consolidation

Consolida memoria giornaliera e appunti grezzi in MEMORY.md.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class DreamingReport:
    updated: bool
    memory_path: str
    consolidated_sources: list[str] = field(default_factory=list)
    lines_added: int = 0

    def render(self) -> str:
        if not self.consolidated_sources:
            return f"Nessuna nuova memoria da consolidare in {self.memory_path}."
        return (
            f"Consolidata memoria in {self.memory_path}: "
            f"{len(self.consolidated_sources)} sorgenti, {self.lines_added} righe aggiunte."
        )


def consolidate_memory(workspace_dir: str, days: int = 7,
                       max_lines_per_file: int = 5,
                       memory_manager=None) -> DreamingReport:
    memory_dir = os.path.join(workspace_dir, "memory")
    memory_path = os.path.join(workspace_dir, "MEMORY.md")
    os.makedirs(memory_dir, exist_ok=True)

    existing = ""
    if os.path.exists(memory_path):
        with open(memory_path, "r", encoding="utf-8") as f:
            existing = f.read()
    else:
        existing = "# MEMORY.md — Private Long-Term Memory\n\n"

    daily_files = []
    for filename in sorted(os.listdir(memory_dir), reverse=True):
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}\.md", filename):
            daily_files.append(filename)
        if len(daily_files) >= max(1, int(days)):
            break

    sections: list[str] = []
    lines_added = 0
    consolidated_sources: list[str] = []

    for filename in reversed(daily_files):
        source_ref = f"memory/{filename}"
        if source_ref in existing:
            continue
        path = os.path.join(memory_dir, filename)
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read()

        bullets = _extract_bullets(raw, max_lines=max_lines_per_file)
        if not bullets:
            continue

        consolidated_sources.append(source_ref)
        lines_added += len(bullets)
        section_lines = [f"### Da {source_ref}"]
        section_lines.extend(f"- {line}" for line in bullets)
        sections.append("\n".join(section_lines))

        # Sogna anche nella memoria semantica: i fatti consolidati
        # diventano ricercabili per significato
        if memory_manager is not None:
            for bullet in bullets:
                try:
                    memory_manager.remember(
                        bullet, category="dreaming",
                        metadata={"source": source_ref},
                    )
                except Exception:
                    pass

    if not sections:
        return DreamingReport(
            updated=False,
            memory_path=memory_path,
            consolidated_sources=[],
            lines_added=0,
        )

    stamp = datetime.now().date().isoformat()
    block = f"\n## Consolidato {stamp}\n\n" + "\n\n".join(sections) + "\n"
    with open(memory_path, "a", encoding="utf-8") as f:
        if existing and not existing.endswith("\n"):
            f.write("\n")
        f.write(block)

    try:
        from core.growth import record_growth_event
        record_growth_event(
            memory_dir, "dreaming",
            f"Consolidate {len(consolidated_sources)} sorgenti, "
            f"{lines_added} fatti in MEMORY.md"
            + (" + memoria semantica" if memory_manager is not None else ""),
        )
    except Exception:
        pass

    return DreamingReport(
        updated=True,
        memory_path=memory_path,
        consolidated_sources=consolidated_sources,
        lines_added=lines_added,
    )


# ── Sogni veri: insight generativi ──

DREAM_PROMPT = """Sei l'agente personale di questo workspace. È notte: rileggi la settimana e sogna — cioè cerca collegamenti che di giorno non vedi.

Materiale della settimana:
{material}

Trova al massimo 3 insight NON OVVI: pattern nel comportamento dell'owner, errori che ripeti, abitudini che emergono, ipotesi su come lavorare meglio insieme. Niente riassunti, niente ovvietà: solo collegamenti veri.

Rispondi SOLO con JSON in questo formato (array, anche vuoto se non c'è nulla di vero da dire):
[
  {{"insight": "il collegamento che hai visto",
    "proposta": "lesson" | "trait" | "none",
    "sezione": "voice|method|owner|identity|boundaries (solo se proposta=trait)",
    "testo": "testo del tratto o della lezione proposta (se proposta != none)"}}
]"""


def _gather_week_material(memory_dir: str, days: int = 7) -> str:
    """Materiale degli ultimi giorni per la passata generativa."""
    import json as _json
    import time as _time

    parts: list[str] = []
    cutoff = _time.time() - days * 86400

    # Note giornaliere recenti
    daily = []
    for filename in sorted(os.listdir(memory_dir), reverse=True):
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}\.md", filename):
            daily.append(filename)
        if len(daily) >= days:
            break
    for filename in reversed(daily):
        try:
            with open(os.path.join(memory_dir, filename), "r", encoding="utf-8") as f:
                content = f.read().strip()
            if content:
                parts.append(f"--- {filename} ---\n{content[:1200]}")
        except Exception:
            continue

    # Eventi di learning della settimana
    events_path = os.path.join(memory_dir, "learning", "events.jsonl")
    if os.path.exists(events_path):
        lines = []
        try:
            with open(events_path, "r", encoding="utf-8") as f:
                for line in f:
                    try:
                        item = _json.loads(line)
                    except Exception:
                        continue
                    if item.get("timestamp", 0) < cutoff:
                        continue
                    lines.append(
                        f"- {item.get('kind')}/{item.get('signal', '')}: "
                        f"{item.get('content', '')[:140]}"
                    )
        except Exception:
            pass
        if lines:
            parts.append("--- segnali di apprendimento ---\n" + "\n".join(lines[-30:]))

    # Riflessioni post-turno della settimana
    refl_dir = os.path.join(memory_dir, "reflections")
    if os.path.isdir(refl_dir):
        refl = []
        for filename in sorted(os.listdir(refl_dir), reverse=True)[:days]:
            try:
                with open(os.path.join(refl_dir, filename), "r", encoding="utf-8") as f:
                    for line in f:
                        try:
                            item = _json.loads(line)
                        except Exception:
                            continue
                        summary = item.get("summary") or item.get("learned") or ""
                        if summary:
                            refl.append(f"- {str(summary)[:140]}")
            except Exception:
                continue
        if refl:
            parts.append("--- riflessioni ---\n" + "\n".join(refl[-20:]))

    return "\n\n".join(parts) if parts else ""


def _parse_dream_json(raw: str) -> list[dict]:
    """Estrae l'array JSON dalla risposta del modello, con tolleranza."""
    import json as _json
    text = (raw or "").strip()
    # Togli eventuali code fence
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    start = text.find("[")
    end = text.rfind("]")
    if start < 0 or end <= start:
        return []
    try:
        data = _json.loads(text[start:end + 1])
    except Exception:
        return []
    return [d for d in data if isinstance(d, dict) and d.get("insight")] \
        if isinstance(data, list) else []


def dream_insights(llm, workspace_dir: str, memory_manager=None,
                   days: int = 7) -> list[dict]:
    """Passata generativa notturna: insight + proposte di lezione/tratto.

    Gli insight vengono salvati in memory/dreams/, indicizzati nella
    memoria semantica e registrati come eventi di learning (le proposte
    di tratto finiscono in anima_proposals.json, da discutere con l'owner).
    """
    memory_dir = os.path.join(workspace_dir, "memory")
    material = _gather_week_material(memory_dir, days=days)
    if not material:
        return []

    try:
        raw = llm.call([{
            "role": "user",
            "content": DREAM_PROMPT.format(material=material[:12000]),
        }])
    except Exception:
        return []

    insights = _parse_dream_json(raw)[:3]
    if not insights:
        return []

    # Salva il sogno
    dreams_dir = os.path.join(memory_dir, "dreams")
    os.makedirs(dreams_dir, exist_ok=True)
    day = datetime.now().date().isoformat()
    dream_path = os.path.join(dreams_dir, f"{day}.md")
    with open(dream_path, "a", encoding="utf-8") as f:
        f.write(f"# Sogno del {day}\n\n")
        for item in insights:
            f.write(f"- {item.get('insight', '')}\n")

    import json as _json
    proposals = []
    for item in insights:
        insight = str(item.get("insight", ""))[:400]

        # Indicizza per significato
        if memory_manager is not None:
            try:
                memory_manager.remember(
                    f"[sogno {day}] {insight}", category="dream",
                    metadata={"day": day},
                )
            except Exception:
                pass

        # Le proposte di tratto vanno discusse con l'owner, non auto-applicate
        if item.get("proposta") == "trait" and item.get("testo"):
            proposals.append({
                "date": day,
                "section": str(item.get("sezione", "method"))[:20],
                "text": str(item.get("testo", ""))[:300],
                "insight": insight,
            })

        # Le proposte di lezione entrano nel learning loop come eventi
        if item.get("proposta") == "lesson" and item.get("testo"):
            try:
                from core.learning import LearningLoop
                LearningLoop(memory_dir).record_event(
                    kind="insight",
                    topic=insight[:100],
                    content=str(item.get("testo", ""))[:500],
                    signal="dream",
                    actor="agent",
                    source="dreaming",
                )
            except Exception:
                pass

    if proposals:
        prop_path = os.path.join(memory_dir, "anima_proposals.json")
        existing = []
        try:
            with open(prop_path, "r", encoding="utf-8") as f:
                existing = _json.load(f)
        except Exception:
            existing = []
        existing.extend(proposals)
        with open(prop_path, "w", encoding="utf-8") as f:
            _json.dump(existing[-20:], f, indent=2, ensure_ascii=False)

    try:
        from core.growth import record_growth_event
        record_growth_event(
            memory_dir, "dream_insight",
            f"{len(insights)} insight dal sogno"
            + (f", {len(proposals)} proposte di tratto" if proposals else ""),
        )
    except Exception:
        pass

    return insights


def _extract_bullets(raw: str, max_lines: int = 5) -> list[str]:
    bullets: list[str] = []
    for line in raw.splitlines():
        text = " ".join(line.strip().split())
        if not text:
            continue
        if text.startswith("#"):
            continue
        if len(text) < 8:
            continue
        if text.lower() in {"todo", "note", "appunti"}:
            continue
        # MEMORY.md è memoria CURATA: i log macchina non sono ricordi.
        # Scarta le righe generate dal runtime (heartbeat/system) e i dump
        # lunghi — un fatto durevole sta in una frase, non in 900 caratteri.
        if "[heartbeat/" in text or "[system/" in text:
            continue
        if "Questo è un heartbeat periodico" in text:
            continue
        if len(text) > 400:
            continue
        bullets.append(text.lstrip("-* ").strip())
        if len(bullets) >= max_lines:
            break
    return bullets
