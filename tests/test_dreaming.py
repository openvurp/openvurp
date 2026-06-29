"""Test per consolidamento memoria lunga."""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.dreaming import consolidate_memory


def test_consolidate_memory_appends_recent_daily_notes_to_memory_md():
    with tempfile.TemporaryDirectory() as tmp:
        os.makedirs(os.path.join(tmp, "memory"), exist_ok=True)
        with open(os.path.join(tmp, "MEMORY.md"), "w", encoding="utf-8") as f:
            f.write("# MEMORY.md\n")
        with open(os.path.join(tmp, "memory", "2026-04-12.md"), "w", encoding="utf-8") as f:
            f.write("Promessa: chiudere il deploy entro sera.\nNota utile: usare processo background.\n")

        report = consolidate_memory(tmp, days=3, max_lines_per_file=3)

        assert report.updated
        with open(os.path.join(tmp, "MEMORY.md"), "r", encoding="utf-8") as f:
            content = f.read()
        assert "memory/2026-04-12.md" in content
        assert "Promessa: chiudere il deploy entro sera." in content


if __name__ == "__main__":
    test_consolidate_memory_appends_recent_daily_notes_to_memory_md()
    print("Tutti i test dreaming passati!")
