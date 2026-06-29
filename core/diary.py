"""
openvurp Core — Diario

Memoria autobiografica in prima persona. A fine giornata, nel ciclo di
dreaming, l'agente rilegge cosa è successo e scrive poche frasi con la
propria voce: cosa ha fatto, dove ha sbagliato, cosa sta capendo.

Non log — biografia. La differenza tra un agente con i log e un agente
che si ricorda com'era.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime

DIARY_DIR = "diary"

DIARY_PROMPT = """Sei l'agente personale di questo workspace. È fine giornata: scrivi la voce del tuo diario.

Materiale di oggi:
{material}

Scrivi 3-5 frasi in PRIMA PERSONA, con la tua voce, come un diario vero:
- cosa hai fatto di significativo con l'owner
- dove hai sbagliato o cosa ti ha corretto (se è successo)
- cosa stai iniziando a capire di lui/lei o di te

Regole: niente elenchi, niente formato report, niente date. Solo prosa
onesta e asciutta. Se la giornata è stata vuota, scrivi una riga sola.
Rispondi SOLO con il testo della voce di diario."""


def _gather_material(memory_dir: str, day: str) -> str:
    """Raccoglie il materiale del giorno per la voce di diario."""
    parts: list[str] = []

    # Nota giornaliera
    daily = os.path.join(memory_dir, f"{day}.md")
    if os.path.exists(daily):
        try:
            with open(daily, "r", encoding="utf-8") as f:
                content = f.read().strip()
            if content:
                parts.append(f"Note del giorno:\n{content[:2000]}")
        except Exception:
            pass

    # Turni del task journal
    journal = os.path.join(memory_dir, "task_journal", f"{day}.jsonl")
    if os.path.exists(journal):
        try:
            turns = []
            with open(journal, "r", encoding="utf-8") as f:
                for line in f:
                    try:
                        item = json.loads(line)
                    except Exception:
                        continue
                    if item.get("type") == "turn_finish":
                        refl = item.get("reflection", {}) or {}
                        summary = refl.get("summary") or refl.get("user_input") or ""
                        if summary:
                            turns.append(f"- {str(summary)[:150]}")
            if turns:
                parts.append("Lavoro svolto:\n" + "\n".join(turns[-12:]))
        except Exception:
            pass

    # Eventi di crescita di oggi
    try:
        from core.growth import read_growth_events
        events = read_growth_events(memory_dir)
        today_events = [
            f"- {e.get('kind')}: {e.get('detail', '')[:100]}"
            for e in events if str(e.get("date", "")).startswith(day)
        ]
        if today_events:
            parts.append("Crescita di oggi:\n" + "\n".join(today_events[-10:]))
    except Exception:
        pass

    # Correzioni/feedback di oggi dal learning
    events_path = os.path.join(memory_dir, "learning", "events.jsonl")
    if os.path.exists(events_path):
        try:
            feedback = []
            with open(events_path, "r", encoding="utf-8") as f:
                for line in f:
                    try:
                        item = json.loads(line)
                    except Exception:
                        continue
                    date = datetime.fromtimestamp(item.get("timestamp", 0))
                    if date.date().isoformat() != day:
                        continue
                    if item.get("kind") in ("user_feedback", "feedback"):
                        feedback.append(f"- {item.get('content', '')[:150]}")
            if feedback:
                parts.append("Feedback ricevuti:\n" + "\n".join(feedback[-6:]))
        except Exception:
            pass

    return "\n\n".join(parts) if parts else "(giornata senza eventi registrati)"


def write_entry(llm, memory_dir: str, day: str = "") -> str:
    """Scrive la voce di diario del giorno. Returns il testo scritto ('' se skip)."""
    day = day or datetime.now().date().isoformat()
    diary_dir = os.path.join(memory_dir, DIARY_DIR)
    os.makedirs(diary_dir, exist_ok=True)

    month_file = os.path.join(diary_dir, f"{day[:7]}.md")

    # Una voce per giorno
    if os.path.exists(month_file):
        try:
            with open(month_file, "r", encoding="utf-8") as f:
                if f"## {day}" in f.read():
                    return ""
        except Exception:
            pass

    material = _gather_material(memory_dir, day)
    prompt = DIARY_PROMPT.format(material=material)

    text = llm.call([{"role": "user", "content": prompt}])
    text = " ".join((text or "").split())
    if not text or len(text) < 20:
        return ""
    if len(text) > 1200:
        text = text[:1200]

    needs_header = not os.path.exists(month_file) or os.path.getsize(month_file) == 0
    with open(month_file, "a", encoding="utf-8") as f:
        if needs_header:
            f.write(f"# Diario — {day[:7]}\n")
        f.write(f"\n## {day}\n\n{text}\n")

    try:
        from core.growth import record_growth_event
        record_growth_event(memory_dir, "diary", f"voce scritta per {day}")
    except Exception:
        pass

    return text


def index_entry(memory_manager, text: str, day: str) -> None:
    """Indicizza la voce nella memoria semantica."""
    if memory_manager is None or not text:
        return
    try:
        memory_manager.remember(
            f"[diario {day}] {text}", category="diary",
            metadata={"day": day},
        )
    except Exception:
        pass


def read_entries(memory_dir: str, limit: int = 7) -> list[tuple[str, str]]:
    """Ultime voci di diario. Returns [(giorno, testo)] dalla più recente."""
    diary_dir = os.path.join(memory_dir, DIARY_DIR)
    if not os.path.isdir(diary_dir):
        return []

    entries: list[tuple[str, str]] = []
    files = sorted(
        (f for f in os.listdir(diary_dir) if re.fullmatch(r"\d{4}-\d{2}\.md", f)),
        reverse=True,
    )
    for filename in files:
        try:
            with open(os.path.join(diary_dir, filename), "r", encoding="utf-8") as f:
                content = f.read()
        except Exception:
            continue
        blocks = re.split(r"^## (\d{4}-\d{2}-\d{2})\s*$", content, flags=re.M)
        # blocks: [preambolo, day1, text1, day2, text2, ...]
        pairs = list(zip(blocks[1::2], blocks[2::2]))
        for day, text in reversed(pairs):
            entries.append((day, text.strip()))
            if len(entries) >= limit:
                return entries
    return entries


def render_diary(memory_dir: str, limit: int = 7) -> str:
    entries = read_entries(memory_dir, limit=limit)
    if not entries:
        return (
            "Empty diary. Entries are born at night, in the dreaming cycle: "
            "the agent rereads the day and tells it in its own voice."
        )
    lines = []
    for day, text in entries:
        lines.append(f"{day}")
        lines.append(f"  {text}")
        lines.append("")
    return "\n".join(lines).rstrip()
