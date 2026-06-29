"""Test per i progetti a lungo termine (core/projects.py)."""

import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.projects import Projects, ProjectError, MAX_ACTIVE_PROJECTS


def _make(tmp):
    return Projects(os.path.join(tmp, "memory"))


def test_create_and_persist():
    with tempfile.TemporaryDirectory() as tmp:
        projects = _make(tmp)
        p = projects.create(
            "Imparare Rust", "Pubblicare una CLI funzionante scritta in Rust",
            why="curriculum", next_step="installare rustup",
        )
        assert p.status == "active"
        assert p.next_step == "installare rustup"

        # Ricarica da disco: sopravvive al "riavvio"
        reloaded = _make(tmp)
        assert len(reloaded.active()) == 1
        assert reloaded.active()[0].title == "Imparare Rust"


def test_create_validation():
    with tempfile.TemporaryDirectory() as tmp:
        projects = _make(tmp)
        with pytest.raises(ProjectError):
            projects.create("ab", "obiettivo abbastanza lungo qui")
        with pytest.raises(ProjectError):
            projects.create("Titolo valido", "corto")


def test_active_cap():
    with tempfile.TemporaryDirectory() as tmp:
        projects = _make(tmp)
        for i in range(MAX_ACTIVE_PROJECTS):
            projects.create(f"Progetto numero {i}", "un obiettivo concreto e misurabile")
        with pytest.raises(ProjectError):
            projects.create("Quello di troppo", "un obiettivo concreto e misurabile")


def test_note_and_next_step():
    with tempfile.TemporaryDirectory() as tmp:
        projects = _make(tmp)
        p = projects.create("Sito personale", "Sito online con dominio custom")
        projects.note(p.id, "comprato il dominio", next_step="scegliere hosting")
        got = projects.get(p.id)
        assert got.log[-1]["note"] == "comprato il dominio"
        assert got.next_step == "scegliere hosting"


def test_milestones():
    with tempfile.TemporaryDirectory() as tmp:
        projects = _make(tmp)
        p = projects.create("Maratona", "Correre la maratona di Roma sotto le 4h")
        projects.milestone_add(p.id, "correre 10km")
        projects.milestone_add(p.id, "correre 21km")
        projects.milestone_done(p.id, "1")
        got = projects.get(p.id)
        done, total = got.progress()
        assert (done, total) == (1, 2)
        # Per titolo (prefisso), e doppio completamento vietato
        projects.milestone_done(p.id, "correre 21")
        with pytest.raises(ProjectError):
            projects.milestone_done(p.id, "1")


def test_lifecycle_pause_resume_complete():
    with tempfile.TemporaryDirectory() as tmp:
        projects = _make(tmp)
        p = projects.create("Pulizia archivio", "Zero file duplicati nel NAS")
        projects.pause(p.id, reason="priorità cambiate")
        assert projects.get(p.id).status == "paused"
        # Niente note su progetti non attivi
        with pytest.raises(ProjectError):
            projects.note(p.id, "avanzamento fantasma")
        projects.resume(p.id)
        projects.complete(p.id, outcome="archivio pulito")
        assert projects.get(p.id).status == "done"
        assert projects.active() == []


def test_prompt_and_heartbeat_rendering():
    with tempfile.TemporaryDirectory() as tmp:
        projects = _make(tmp)
        assert projects.compile_prompt() == ""
        assert projects.heartbeat_state() == ""
        p = projects.create("Orto sul balcone", "Raccogliere i primi pomodori",
                            next_step="comprare i vasi")
        prompt = projects.compile_prompt()
        assert "PROGETTI IN CORSO" in prompt
        assert "Orto sul balcone" in prompt
        assert "comprare i vasi" in prompt
        hb = projects.heartbeat_state()
        assert p.id in hb
        status = projects.render_status()
        assert "Orto sul balcone" in status
