"""
openvurp Core — Vector Memory

Memoria con embedding vettoriali via SQLite + FTS5.
Embedding via Ollama /api/embed (locale) o fallback a solo FTS5.
"""

from __future__ import annotations

import os
import json
import math
import struct
import sqlite3
import time
from datetime import datetime
from typing import Optional


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity tra due vettori."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def _serialize_embedding(embedding: list[float]) -> bytes:
    """Serializza embedding come BLOB (array di float32)."""
    return struct.pack(f'{len(embedding)}f', *embedding)


def _deserialize_embedding(blob: bytes) -> list[float]:
    """Deserializza embedding da BLOB."""
    if not blob:
        return []
    n = len(blob) // 4
    return list(struct.unpack(f'{n}f', blob))


class VectorMemory:
    """Memoria vettoriale con SQLite + FTS5 + embedding opzionali."""

    def __init__(self, db_path: str,
                 embedding_provider: str = "ollama",
                 embedding_model: str = "nomic-embed-text",
                 embedding_base_url: str = "http://localhost:11434"):
        self.db_path = db_path
        self.embedding_provider = embedding_provider
        self.embedding_model = embedding_model
        self.embedding_base_url = embedding_base_url
        self._embeddings_available = None  # Lazy check

        # check_same_thread=False: i canali (telegram, ecc.) girano in thread diversi
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self):
        """Crea tabelle se non esistono."""
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content TEXT NOT NULL,
                category TEXT DEFAULT 'general',
                embedding BLOB,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                accessed_at TIMESTAMP,
                access_count INTEGER DEFAULT 0,
                metadata TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_memories_category ON memories(category);
            CREATE INDEX IF NOT EXISTS idx_memories_created ON memories(created_at);
        """)

        # FTS5 — creazione separata (non supporta IF NOT EXISTS in tutte le versioni)
        try:
            self.db.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts "
                "USING fts5(content, category, content='memories', content_rowid='id')"
            )
        except sqlite3.OperationalError:
            pass  # Già esiste o FTS5 non disponibile

        self.db.commit()

    def _check_embeddings(self) -> bool:
        """Verifica se il provider di embedding è disponibile."""
        if self._embeddings_available is not None:
            return self._embeddings_available

        try:
            emb = self._get_embedding("test")
            self._embeddings_available = len(emb) > 0
        except Exception:
            self._embeddings_available = False

        return self._embeddings_available

    def _get_embedding(self, text: str) -> list[float]:
        """Ottieni embedding per un testo."""
        if self.embedding_provider == "ollama":
            return self._embed_ollama(text)
        elif self.embedding_provider == "openai":
            return self._embed_openai(text)
        return []

    def _embed_ollama(self, text: str) -> list[float]:
        """Embedding via Ollama /api/embed."""
        import requests
        r = requests.post(
            f"{self.embedding_base_url}/api/embed",
            json={"model": self.embedding_model, "input": text},
            timeout=10
        )
        r.raise_for_status()
        data = r.json()
        # Ollama restituisce {"embeddings": [[...]]}
        embeddings = data.get("embeddings", [])
        if embeddings:
            return embeddings[0]
        return []

    def _embed_openai(self, text: str) -> list[float]:
        """Embedding via OpenAI API."""
        from openai import OpenAI
        client = OpenAI()
        r = client.embeddings.create(
            model=self.embedding_model or "text-embedding-3-small",
            input=text
        )
        return r.data[0].embedding

    def add(self, content: str, category: str = "general", metadata: dict = None) -> int:
        """Aggiunge un ricordo alla memoria."""
        embedding = None
        if self._check_embeddings():
            try:
                emb = self._get_embedding(content)
                embedding = _serialize_embedding(emb) if emb else None
            except Exception:
                pass

        meta_json = json.dumps(metadata, ensure_ascii=False) if metadata else None

        cursor = self.db.execute(
            "INSERT INTO memories (content, category, embedding, metadata) VALUES (?, ?, ?, ?)",
            (content, category, embedding, meta_json)
        )
        mem_id = cursor.lastrowid

        # Aggiorna FTS5
        try:
            self.db.execute(
                "INSERT INTO memories_fts (rowid, content, category) VALUES (?, ?, ?)",
                (mem_id, content, category)
            )
        except sqlite3.OperationalError:
            pass

        self.db.commit()
        return mem_id

    @staticmethod
    def _fts_query(query: str) -> str:
        """La domanda, tradotta in qualcosa che FTS5 possa davvero trovare.

        FTS5 mette in AND le parole: `MATCH 'Crucial SSD prezzo'` pretende che
        il ricordo contenga tutte e tre. Una domanda in lingua non ha quasi mai
        tutte le parole del ricordo, quindi la ricerca tornava vuota quasi
        sempre — e `remember` continuava a rispondere «salvato» per una cosa che
        poi nessuno ritrovava.

        In OR invece basta una parola in comune, e il punteggio (bm25) mette
        avanti chi ne ha di piu'. Le virgolette servono anche a un altro
        guasto: un apostrofo nella domanda faceva esplodere la sintassi FTS5,
        l'errore veniva ingoiato, e il risultato era di nuovo zero ricordi.
        """
        import re
        parole = [p for p in re.findall(r"\w+", str(query or ""), re.UNICODE)
                  if len(p) > 2]
        if not parole:
            return ""
        return " OR ".join(f'"{p}"' for p in parole[:12])

    def search(self, query: str, top_k: int = 5, min_score: float = 0.3) -> list[dict]:
        """Ricerca ibrida: vector similarity + FTS5 + temporal decay."""
        results = {}

        # 1. FTS5 keyword search
        try:
            fts_query = self._fts_query(query)
            fts_rows = self.db.execute(
                "SELECT rowid, rank FROM memories_fts WHERE memories_fts MATCH ? ORDER BY rank LIMIT ?",
                (fts_query, top_k * 2)
            ).fetchall() if fts_query else []

            if fts_rows:
                # FTS5 bm25 rank è negativo: più negativo = match migliore,
                # quindi abs(rank) più alto = migliore. Normalizza sul massimo
                # così il best match vale 1.0.
                max_rank = max(abs(r['rank']) for r in fts_rows) or 1.0
                for row in fts_rows:
                    rid = row['rowid']
                    fts_score = abs(row['rank']) / max_rank
                    results[rid] = {"fts_score": fts_score, "vec_score": 0.0}
        except sqlite3.OperationalError:
            pass

        # 2. Vector similarity (se embeddings disponibili)
        query_embedding = None
        if self._check_embeddings():
            try:
                query_embedding = self._get_embedding(query)
            except Exception:
                pass

        if query_embedding:
            all_rows = self.db.execute(
                "SELECT id, embedding FROM memories WHERE embedding IS NOT NULL"
            ).fetchall()

            for row in all_rows:
                emb = _deserialize_embedding(row['embedding'])
                if emb:
                    sim = _cosine_similarity(query_embedding, emb)
                    if row['id'] in results:
                        results[row['id']]['vec_score'] = sim
                    else:
                        results[row['id']] = {"fts_score": 0.0, "vec_score": sim}

        # 3. Calcola score combinato con temporal decay
        now = time.time()
        scored = []

        for mem_id, scores in results.items():
            row = self.db.execute(
                "SELECT * FROM memories WHERE id = ?", (mem_id,)
            ).fetchone()
            if not row:
                continue

            # Score ibrido: vector 0.7, FTS 0.3
            if query_embedding:
                combined = scores['vec_score'] * 0.7 + scores['fts_score'] * 0.3
            else:
                combined = scores['fts_score']

            # Temporal decay (half-life 30 giorni)
            created = row['created_at']
            if created:
                try:
                    created_ts = datetime.fromisoformat(created).timestamp()
                    age_days = (now - created_ts) / 86400
                    decay = math.pow(0.5, age_days / 30.0)
                    combined *= (0.7 + 0.3 * decay)  # Decay influenza solo 30%
                except Exception:
                    pass

            if combined >= min_score:
                scored.append({
                    "id": row['id'],
                    "content": row['content'],
                    "category": row['category'],
                    "score": round(combined, 4),
                    "created_at": row['created_at'],
                    "access_count": row['access_count'],
                    "metadata": json.loads(row['metadata']) if row['metadata'] else None,
                })

        # Sort by score e applica MMR per ridurre ridondanza
        scored.sort(key=lambda x: x['score'], reverse=True)
        final = self._mmr_rerank(scored, top_k)

        # Aggiorna access stats
        for item in final:
            self.db.execute(
                "UPDATE memories SET accessed_at = ?, access_count = access_count + 1 WHERE id = ?",
                (datetime.now().isoformat(), item['id'])
            )
        self.db.commit()

        return final

    def _mmr_rerank(self, results: list[dict], top_k: int, diversity: float = 0.3) -> list[dict]:
        """Maximal Marginal Relevance per ridurre ridondanza."""
        if len(results) <= top_k:
            return results

        selected = [results[0]]
        remaining = results[1:]

        while len(selected) < top_k and remaining:
            best_idx = 0
            best_mmr = -1

            for i, candidate in enumerate(remaining):
                # Similarità con già selezionati (approssimata con overlap parole)
                max_sim = 0
                c_words = set(candidate['content'].lower().split())
                for s in selected:
                    s_words = set(s['content'].lower().split())
                    overlap = len(c_words & s_words) / max(len(c_words | s_words), 1)
                    max_sim = max(max_sim, overlap)

                mmr = (1 - diversity) * candidate['score'] - diversity * max_sim
                if mmr > best_mmr:
                    best_mmr = mmr
                    best_idx = i

            selected.append(remaining.pop(best_idx))

        return selected

    def fade(self, archive_path: str, max_idle_days: int = 45,
             min_access: int = 2, max_per_run: int = 30,
             protected_categories: tuple = ("lesson", "identity", "pact")) -> list[dict]:
        """L'arte di dimenticare: i ricordi mai più toccati sbiadiscono.

        Un ricordo sbiadisce se: è più vecchio di max_idle_days, non è
        stato richiamato negli ultimi max_idle_days, ed è stato richiamato
        meno di min_access volte in tutta la sua vita. I richiami (search)
        lo rinforzano: accessed_at/access_count si aggiornano a ogni hit.

        Non è una cancellazione: lo sbiadito finisce in un archivio JSONL
        (recuperabile a mano). Ritorna i ricordi sbiaditi.
        """
        from datetime import timedelta
        cutoff = (datetime.now() - timedelta(days=max_idle_days)).isoformat()
        placeholders = ",".join("?" for _ in protected_categories)
        rows = self.db.execute(
            f"""SELECT * FROM memories
                WHERE created_at < ?
                  AND (accessed_at IS NULL OR accessed_at < ?)
                  AND access_count < ?
                  AND category NOT IN ({placeholders})
                ORDER BY created_at ASC
                LIMIT ?""",
            (cutoff, cutoff, min_access, *protected_categories, max_per_run),
        ).fetchall()
        if not rows:
            return []

        faded = []
        os.makedirs(os.path.dirname(archive_path) or ".", exist_ok=True)
        with open(archive_path, "a", encoding="utf-8") as f:
            for row in rows:
                record = {
                    "content": row["content"],
                    "category": row["category"],
                    "created_at": row["created_at"],
                    "access_count": row["access_count"],
                    "faded_at": datetime.now().isoformat(timespec="seconds"),
                }
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
                faded.append(record)
                self.db.execute("DELETE FROM memories WHERE id = ?", (row["id"],))
                try:
                    self.db.execute(
                        "DELETE FROM memories_fts WHERE rowid = ?", (row["id"],)
                    )
                except sqlite3.OperationalError:
                    pass
        self.db.commit()
        return faded

    def forget(self, memory_id: int) -> bool:
        """Rimuove un ricordo."""
        cursor = self.db.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
        try:
            self.db.execute("DELETE FROM memories_fts WHERE rowid = ?", (memory_id,))
        except sqlite3.OperationalError:
            pass
        self.db.commit()
        return cursor.rowcount > 0

    def stats(self) -> dict:
        """Statistiche della memoria vettoriale."""
        total = self.db.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        with_emb = self.db.execute(
            "SELECT COUNT(*) FROM memories WHERE embedding IS NOT NULL"
        ).fetchone()[0]
        categories = self.db.execute(
            "SELECT category, COUNT(*) as cnt FROM memories GROUP BY category"
        ).fetchall()

        return {
            "total_memories": total,
            "with_embeddings": with_emb,
            "categories": {r['category']: r['cnt'] for r in categories},
            "embeddings_available": self._check_embeddings(),
            "db_path": self.db_path,
        }

    def close(self):
        """Chiude connessione DB."""
        self.db.close()
