"""
openvurp Core — Memory Manager

Memoria strutturata con profile, lessons, patterns, retrieval selettivo.
"""

from __future__ import annotations

import os
import json
import re
from datetime import datetime
from typing import Optional


class MemoryManager:
    def __init__(self, base_dir: str):
        self.base_dir = base_dir
        os.makedirs(base_dir, exist_ok=True)
        os.makedirs(os.path.join(base_dir, "lessons"), exist_ok=True)
        os.makedirs(os.path.join(base_dir, "projects"), exist_ok=True)
        os.makedirs(os.path.join(base_dir, "sessions"), exist_ok=True)
        self._ensure_scaffold()
        self.vector = self._init_vector_memory()

    def _init_vector_memory(self):
        """Memoria semantica (SQLite + FTS5 + embedding opzionali).

        Graceful: se disabilitata o non inizializzabile, il retrieval
        resta solo keyword-based.
        """
        try:
            import config as cfg
            if not getattr(cfg, "VECTOR_MEMORY_ENABLED", True):
                return None
            from core.vector_memory import VectorMemory
            return VectorMemory(
                os.path.join(self.base_dir, "vector_memory.db"),
                embedding_provider=getattr(cfg, "EMBEDDING_PROVIDER", "ollama"),
                embedding_model=getattr(cfg, "EMBEDDING_MODEL", "nomic-embed-text"),
                embedding_base_url=getattr(cfg, "EMBEDDING_BASE_URL", "http://localhost:11434"),
            )
        except Exception:
            return None

    def remember(self, content: str, category: str = "general",
                 metadata: dict = None) -> bool:
        """Indicizza un ricordo nella memoria semantica."""
        if not self.vector or not (content or "").strip():
            return False
        try:
            self.vector.add(content.strip(), category=category, metadata=metadata)
            return True
        except Exception:
            return False

    def fade_memories(self) -> int:
        """Sbiadimento notturno: i ricordi mai richiamati lasciano spazio.

        Un agente che ricorda tutto non capisce niente. I ricordi sbiaditi
        finiscono in memory/.faded/faded.jsonl, non nel nulla.
        Ritorna quanti ricordi sono sbiaditi in questa passata.
        """
        if not self.vector:
            return 0
        try:
            import config as cfg
            if not getattr(cfg, "MEMORY_FADE_ENABLED", True):
                return 0
            idle_days = int(getattr(cfg, "MEMORY_FADE_IDLE_DAYS", 45))
        except Exception:
            idle_days = 45
        try:
            archive = os.path.join(self.base_dir, ".faded", "faded.jsonl")
            faded = self.vector.fade(archive, max_idle_days=idle_days)
            if faded:
                try:
                    from core.growth import record_growth_event
                    record_growth_event(
                        self.base_dir, "memory",
                        f"{len(faded)} ricordi sbiaditi (mai più richiamati)",
                    )
                except Exception:
                    pass
            return len(faded)
        except Exception:
            return 0

    def _ensure_scaffold(self):
        """Crea i file base della memoria se mancano ancora."""
        defaults = {
            "profilo.json": {},
            "environment.json": {},
            "patterns.json": {},
        }
        for relative_path, data in defaults.items():
            path = os.path.join(self.base_dir, relative_path)
            if os.path.exists(path):
                continue
            self._save_json(relative_path, data)

    # ── Profile ──

    def get_profile(self) -> dict:
        return self._load_json("profilo.json", {})

    def set_profile(self, data: dict):
        self._save_json("profilo.json", data)

    def update_profile(self, key: str, value):
        profile = self.get_profile()
        profile[key] = value
        self.set_profile(profile)

    # ── Lessons ──

    def get_lessons(self) -> list[dict]:
        """Carica tutte le lezioni."""
        lessons_dir = os.path.join(self.base_dir, "lessons")
        lessons = []
        if not os.path.exists(lessons_dir):
            return lessons

        for f in sorted(os.listdir(lessons_dir)):
            if f.endswith(('.md', '.json')):
                path = os.path.join(lessons_dir, f)
                try:
                    if f.endswith('.json'):
                        with open(path, "r", encoding="utf-8") as fh:
                            lessons.append(json.load(fh))
                    else:
                        with open(path, "r", encoding="utf-8") as fh:
                            content = fh.read()
                        lessons.append({
                            "file": f,
                            "content": content,
                            "date": f[:10] if len(f) > 10 else ""
                        })
                except Exception:
                    continue
        return lessons

    def store_lesson(self, topic: str, content: str, tags: list[str] = None):
        """Salva una nuova lezione."""
        date = datetime.now().strftime("%Y-%m-%d")
        slug = re.sub(r'[^a-z0-9]+', '-', topic.lower()).strip('-')[:50]
        filename = f"{date}_{slug}.md"
        path = os.path.join(self.base_dir, "lessons", filename)

        header = f"# {topic}\n"
        if tags:
            header += f"tags: {', '.join(tags)}\n"
        header += f"data: {date}\n\n"

        with open(path, "w", encoding="utf-8") as f:
            f.write(header + content)

        # Indicizza anche nella memoria semantica per il retrieval ibrido
        self.remember(f"{topic}\n{content}", category="lesson",
                      metadata={"tags": tags or [], "file": filename})

    # ── Patterns ──

    def get_patterns(self) -> dict:
        return self._load_json("patterns.json", {})

    def update_pattern(self, key: str, value):
        patterns = self.get_patterns()
        patterns[key] = value
        self._save_json("patterns.json", patterns)

    # ── Environment ──

    def get_environment(self) -> dict:
        return self._load_json("environment.json", {})

    def set_environment(self, data: dict):
        self._save_json("environment.json", data)

    # ── Projects ──

    def get_project(self, name: str) -> dict:
        return self._load_json(os.path.join("projects", f"{name}.json"), {})

    def set_project(self, name: str, data: dict):
        self._save_json(os.path.join("projects", f"{name}.json"), data)

    # ── Generic store/recall ──

    def store(self, category: str, key: str, value):
        """Salva un valore in una categoria."""
        data = self._load_json(f"{category}.json", {})
        data[key] = value
        self._save_json(f"{category}.json", data)

    def recall(self, category: str, key: str = None):
        """Recupera un valore o tutta la categoria."""
        data = self._load_json(f"{category}.json", {})
        if key:
            return data.get(key)
        return data

    def forget(self, category: str, key: str):
        """Rimuove un valore da una categoria."""
        data = self._load_json(f"{category}.json", {})
        data.pop(key, None)
        self._save_json(f"{category}.json", data)

    # ── Retrieval selettivo ──

    def get_relevant(self, user_input: str, budget_chars: int = 8000,
                     session_type: str = "main") -> str:
        """Seleziona memorie rilevanti per l'input, rispettando budget."""
        if session_type != "main":
            return "(nessun ricordo ancora)"

        include_private = session_type == "main"

        if not user_input:
            return self._get_all_memories(budget_chars, include_private=include_private)

        keywords = self._extract_keywords(user_input.lower())
        scored = []

        # Score ogni file in memoria
        for relative_path, content in self._iter_searchable_memories(include_private=include_private):
            if not self._is_meaningful_memory_content(content):
                continue

            # Score basato su keyword match
            score = 0
            content_lower = content.lower()
            for kw in keywords:
                if kw in content_lower:
                    score += content_lower.count(kw)
                if kw in relative_path.lower():
                    score += 5  # Bonus per match nel filename

            # Profilo sempre rilevante
            if include_private and relative_path == "profilo.json":
                score += 10
            elif include_private and relative_path.startswith("sessions/"):
                score += 2

            scored.append((score, relative_path, content))

        # Sort by score descending
        scored.sort(key=lambda x: x[0], reverse=True)

        # Build output rispettando budget
        output_parts = []
        used_chars = 0

        for score, filename, content in scored:
            if score == 0 and used_chars > budget_chars * 0.3:
                continue  # Skip irrilevanti se abbiamo già abbastanza

            entry = f"\n### memory/{filename}\n```\n"
            if len(content) > 2000:
                content = content[:2000] + "\n[...troncato]"
            entry += content + "\n```\n"

            if used_chars + len(entry) > budget_chars:
                if used_chars == 0:
                    # Almeno il primo file, troncato
                    entry = entry[:budget_chars]
                else:
                    break

            output_parts.append(entry)
            used_chars += len(entry)

        # Ricerca semantica (vector + FTS5) sui ricordi indicizzati
        semantic = self._semantic_section(user_input, budget_chars - used_chars)
        if semantic:
            output_parts.append(semantic)

        return "".join(output_parts) if output_parts else "(nessun ricordo ancora)"

    def _semantic_section(self, user_input: str, budget_chars: int) -> str:
        """Sezione con i ricordi semanticamente più vicini all'input."""
        if not self.vector or budget_chars < 200:
            return ""
        try:
            hits = self.vector.search(user_input, top_k=5)
        except Exception:
            return ""
        if not hits:
            return ""

        lines = ["\n### ricordi semantici\n"]
        used = len(lines[0])
        for hit in hits:
            content = (hit.get("content") or "").strip()
            if not content:
                continue
            if len(content) > 500:
                content = content[:500] + " [...]"
            entry = f"- ({hit.get('category', 'general')}, score {hit.get('score', 0)}) {content}\n"
            if used + len(entry) > budget_chars:
                break
            lines.append(entry)
            used += len(entry)

        return "".join(lines) if len(lines) > 1 else ""

    def _get_all_memories(self, budget_chars: int, include_private: bool = True) -> str:
        """Carica tutte le memorie (fallback senza input)."""
        parts = []
        used = 0

        for relative_path, content in self._iter_searchable_memories(include_private=include_private):
            if not self._is_meaningful_memory_content(content):
                continue

            if len(content) > 2000:
                content = content[:2000] + "\n[...troncato]"

            entry = f"\n### memory/{relative_path}\n```\n{content}\n```\n"
            if used + len(entry) > budget_chars:
                break
            parts.append(entry)
            used += len(entry)

        return "".join(parts) if parts else "(nessun ricordo ancora)"

    def _extract_keywords(self, text: str) -> list[str]:
        """Estrai keyword significative dall'input."""
        # Rimuovi stopwords italiane/inglesi comuni
        stopwords = {
            'il', 'lo', 'la', 'i', 'gli', 'le', 'un', 'una', 'di', 'a', 'da',
            'in', 'con', 'su', 'per', 'tra', 'fra', 'che', 'non', 'si', 'come',
            'ma', 'se', 'mi', 'ti', 'ci', 'vi', 'ne', 'lo', 'li', 'me', 'te',
            'e', 'o', 'è', 'sono', 'hai', 'ho', 'ha', 'cosa', 'questo', 'quello',
            'the', 'a', 'an', 'is', 'in', 'to', 'of', 'and', 'or', 'for', 'it',
            'my', 'your', 'do', 'can', 'how', 'what', 'this', 'that', 'with',
            'puoi', 'fammi', 'dimmi', 'fai', 'vorrei', 'voglio', 'posso',
        }

        words = re.findall(r'[a-zA-Zà-ú]{3,}', text)
        return [w for w in words if w not in stopwords]

    def _list_memory_files(self) -> list[str]:
        """Lista file di memoria (top-level)."""
        files = []
        for f in sorted(os.listdir(self.base_dir)):
            path = os.path.join(self.base_dir, f)
            if os.path.isfile(path):
                files.append(f)
        return files

    def _iter_searchable_memories(self, include_private: bool = True) -> list[tuple[str, str]]:
        """Restituisce i documenti di memoria utili al retrieval."""
        entries: list[tuple[str, str]] = []

        for filename in self._list_memory_files():
            if not include_private:
                if filename == "profilo.json":
                    continue
                if re.fullmatch(r"\d{4}-\d{2}-\d{2}\.md", filename):
                    continue
            path = os.path.join(self.base_dir, filename)
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    entries.append((filename, fh.read()))
            except Exception:
                continue

        for subdir in ("lessons", "projects", "task_journal", "reflections"):
            if not include_private and subdir in ("task_journal", "reflections"):
                continue
            folder = os.path.join(self.base_dir, subdir)
            if not os.path.exists(folder):
                continue
            for filename in sorted(os.listdir(folder)):
                path = os.path.join(folder, filename)
                if not os.path.isfile(path):
                    continue
                try:
                    with open(path, "r", encoding="utf-8") as fh:
                        entries.append((os.path.join(subdir, filename), fh.read()))
                except Exception:
                    continue

        sessions_dir = os.path.join(self.base_dir, "sessions")
        if include_private and os.path.exists(sessions_dir):
            session_files = []
            for filename in os.listdir(sessions_dir):
                path = os.path.join(sessions_dir, filename)
                if os.path.isfile(path):
                    session_files.append((os.path.getmtime(path), filename, path))

            for _, filename, path in sorted(session_files, reverse=True)[:8]:
                try:
                    with open(path, "r", encoding="utf-8") as fh:
                        raw = json.load(fh)
                except Exception:
                    continue
                summary = self._format_session_memory(raw)
                if summary:
                    entries.append((os.path.join("sessions", filename), summary))

        return entries

    def _format_session_memory(self, raw: dict) -> str:
        """Converte un summary sessione in testo compatto ricercabile."""
        if not isinstance(raw, dict):
            return ""

        lines = []
        for key in ("id", "started_at", "turns"):
            value = raw.get(key)
            if value in (None, "", [], {}):
                continue
            lines.append(f"{key}: {value}")

        # Usa conversazione completa se disponibile, altrimenti preview
        conversation = raw.get("conversation")
        if isinstance(conversation, list) and conversation:
            lines.append("conversazione:")
            for item in conversation:
                if not isinstance(item, dict):
                    continue
                role = item.get("role", "?")
                text = item.get("text", "")
                if text:
                    lines.append(f"  {role}: {text}")
        else:
            # Fallback per sessioni vecchie senza conversation
            previews = raw.get("recent_messages")
            if isinstance(previews, list) and previews:
                lines.append("messaggi:")
                for item in previews[:4]:
                    if not isinstance(item, dict):
                        continue
                    role = item.get("role", "?")
                    preview = item.get("preview", "")
                    if preview:
                        lines.append(f"  {role}: {preview}")

        return "\n".join(lines)

    def _is_meaningful_memory_content(self, content: str) -> bool:
        """Evita di iniettare file vuoti o scaffold non ancora popolato."""
        stripped = content.strip()
        return stripped not in ("", "{}", "[]", '""')

    # ── Auto-learning ──

    def auto_learn_prompt(self) -> str:
        """Genera prompt per far estrarre lezioni all'LLM."""
        return (
            "\n[Se durante questa sessione hai imparato qualcosa di nuovo "
            "sull'utente, sul sistema, o su come fare qualcosa, "
            "salvalo nella memoria. Usa il terminale per scrivere file in "
            f"{self.base_dir}/. "
            "Non serve salvare cose ovvie — solo insight utili per le prossime sessioni.]"
        )

    # ── Helpers JSON ──

    def _load_json(self, relative_path: str, default=None):
        path = os.path.join(self.base_dir, relative_path)
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return default if default is not None else {}

    def _save_json(self, relative_path: str, data):
        path = os.path.join(self.base_dir, relative_path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    # ── Cleanup e TTL ──

    def cleanup(self, max_lessons_age_days: int = 90, max_sessions: int = 50):
        """Pulisce memoria vecchia: lezioni scadute, sessioni in eccesso."""
        import time

        now = time.time()
        max_age = max_lessons_age_days * 86400
        removed = 0

        # Lezioni vecchie
        lessons_dir = os.path.join(self.base_dir, "lessons")
        if os.path.exists(lessons_dir):
            for f in os.listdir(lessons_dir):
                path = os.path.join(lessons_dir, f)
                if os.path.isfile(path):
                    age = now - os.path.getmtime(path)
                    if age > max_age:
                        os.remove(path)
                        removed += 1

        # Sessioni in eccesso (tieni le piu recenti)
        sessions_dir = os.path.join(self.base_dir, "sessions")
        if os.path.exists(sessions_dir):
            files = []
            for f in os.listdir(sessions_dir):
                path = os.path.join(sessions_dir, f)
                if os.path.isfile(path):
                    files.append((os.path.getmtime(path), path))
            files.sort(reverse=True)
            for _, path in files[max_sessions:]:
                os.remove(path)
                removed += 1

        # Pattern vuoti
        patterns = self.get_patterns()
        if patterns:
            cleaned = {k: v for k, v in patterns.items() if v}
            if len(cleaned) < len(patterns):
                self._save_json("patterns.json", cleaned)

        return removed

    # ── Stats per UI ──

    def stats(self) -> dict:
        """Statistiche memoria per display."""
        files = self._list_memory_files()
        lessons = len(os.listdir(os.path.join(self.base_dir, "lessons"))) \
            if os.path.exists(os.path.join(self.base_dir, "lessons")) else 0
        projects = len(os.listdir(os.path.join(self.base_dir, "projects"))) \
            if os.path.exists(os.path.join(self.base_dir, "projects")) else 0

        total_size = sum(
            os.path.getsize(os.path.join(self.base_dir, f))
            for f in files if os.path.isfile(os.path.join(self.base_dir, f))
        )

        return {
            "files": len(files),
            "lessons": lessons,
            "projects": projects,
            "total_size": total_size,
            "has_profile": os.path.exists(os.path.join(self.base_dir, "profilo.json")),
        }
