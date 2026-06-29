"""Test per l'arte di dimenticare (VectorMemory.fade)."""

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.vector_memory import VectorMemory


def _make(tmp) -> VectorMemory:
    vm = VectorMemory(os.path.join(tmp, "vm.db"), embedding_provider="none")
    vm._embeddings_available = False  # niente rete nei test
    return vm


def _backdate(vm: VectorMemory, mem_id: int, days: int):
    old = (datetime.now() - timedelta(days=days)).isoformat()
    vm.db.execute("UPDATE memories SET created_at = ? WHERE id = ?", (old, mem_id))
    vm.db.commit()


def test_old_unrecalled_memory_fades_to_archive():
    with tempfile.TemporaryDirectory() as tmp:
        vm = _make(tmp)
        archive = os.path.join(tmp, ".faded", "faded.jsonl")
        mem_id = vm.add("dettaglio effimero mai più servito", category="general")
        _backdate(vm, mem_id, days=90)

        faded = vm.fade(archive, max_idle_days=45)
        assert len(faded) == 1
        assert faded[0]["content"] == "dettaglio effimero mai più servito"
        # Sparito dal DB attivo, presente nell'archivio
        assert vm.stats()["total_memories"] == 0
        with open(archive, encoding="utf-8") as f:
            rows = [json.loads(line) for line in f]
        assert rows[0]["content"] == "dettaglio effimero mai più servito"
        assert rows[0]["faded_at"]


def test_recent_memory_survives():
    with tempfile.TemporaryDirectory() as tmp:
        vm = _make(tmp)
        vm.add("ricordo fresco di oggi", category="general")
        faded = vm.fade(os.path.join(tmp, "f.jsonl"), max_idle_days=45)
        assert faded == []
        assert vm.stats()["total_memories"] == 1


def test_recalled_memory_is_reinforced_and_survives():
    with tempfile.TemporaryDirectory() as tmp:
        vm = _make(tmp)
        mem_id = vm.add("preferenza importante richiamata spesso", category="general")
        _backdate(vm, mem_id, days=90)
        # Richiami recenti (access_count >= 2 e accessed_at fresco)
        now = datetime.now().isoformat()
        vm.db.execute(
            "UPDATE memories SET accessed_at = ?, access_count = 5 WHERE id = ?",
            (now, mem_id),
        )
        vm.db.commit()
        faded = vm.fade(os.path.join(tmp, "f.jsonl"), max_idle_days=45)
        assert faded == []
        assert vm.stats()["total_memories"] == 1


def test_protected_categories_never_fade():
    with tempfile.TemporaryDirectory() as tmp:
        vm = _make(tmp)
        for category in ("lesson", "identity", "pact"):
            mem_id = vm.add(f"ricordo {category} antichissimo", category=category)
            _backdate(vm, mem_id, days=400)
        faded = vm.fade(os.path.join(tmp, "f.jsonl"), max_idle_days=45)
        assert faded == []
        assert vm.stats()["total_memories"] == 3


def test_search_reinforces_access_stats():
    with tempfile.TemporaryDirectory() as tmp:
        vm = _make(tmp)
        vm.add("il gatto di Mario si chiama Felix", category="general")
        results = vm.search("gatto Felix", top_k=3, min_score=0.0)
        assert results
        row = vm.db.execute("SELECT access_count, accessed_at FROM memories").fetchone()
        assert row["access_count"] >= 1
        assert row["accessed_at"]
