"""Test per la fucina (core/forge.py): il ciclo proposta→test→adozione."""

import json
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.forge import Forge, ForgeError


GOOD_PLUGIN = '''
from core.tools import ToolResult


def register(_manager):
    return None


def saluta_handler(**kwargs):
    return ToolResult.ok("ciao")


def selftest():
    r = saluta_handler()
    assert r.success and r.output == "ciao"
    return True
'''

BAD_PLUGIN = '''
def selftest():
    raise RuntimeError("non funziona")
'''

NO_SELFTEST_PLUGIN = '''
def register(_manager):
    return None
'''


def _write_plugin(openvurp_dir: str, plugin_id: str, code: str):
    plugin_dir = os.path.join(openvurp_dir, "plugins", plugin_id)
    os.makedirs(plugin_dir, exist_ok=True)
    with open(os.path.join(plugin_dir, "__init__.py"), "w", encoding="utf-8") as f:
        f.write(code)
    manifest = {"id": plugin_id, "name": plugin_id, "tools": [], "enabled": True}
    with open(os.path.join(plugin_dir, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f)


def _make(tmp):
    # openvurp_dir = repo vero (serve core.tools importabile dal selftest),
    # ma plugins/ e memoria in tmp per non sporcare il workspace.
    return Forge(os.path.join(tmp, "memory"), tmp)


def test_propose_validation():
    with tempfile.TemporaryDirectory() as tmp:
        forge = _make(tmp)
        with pytest.raises(ForgeError):
            forge.propose("bad-id!", "mi serve leggere i feed RSS dei blog che seguo")
        with pytest.raises(ForgeError):
            forge.propose("rss_reader", "troppo corto")
        e = forge.propose("rss_reader", "mi serve leggere i feed RSS dei blog che seguo")
        assert e.status == "proposed"
        # Doppia proposta per lo stesso plugin: bloccata
        with pytest.raises(ForgeError):
            forge.propose("rss_reader", "mi serve di nuovo la stessa identica cosa")


def test_full_lifecycle_pass():
    with tempfile.TemporaryDirectory() as tmp:
        forge = _make(tmp)
        e = forge.propose("saluti", "mi serve un tool che saluta per testare la fucina")
        # Draft senza codice: bloccato
        with pytest.raises(ForgeError):
            forge.mark_drafted(e.id)
        _write_plugin(tmp, "saluti", GOOD_PLUGIN)
        forge.mark_drafted(e.id)
        # Adozione senza test: bloccata
        with pytest.raises(ForgeError):
            forge.adopt(e.id)
        tested = forge.test(e.id)
        assert tested.status == "tested"
        assert tested.code_hash
        adopted = forge.adopt(e.id)
        assert adopted.status == "adopted"
        assert adopted.adopted_at
        # Provenance nel rendering
        assert "saluti" in forge.render_status()


def test_selftest_failure_blocks():
    with tempfile.TemporaryDirectory() as tmp:
        forge = _make(tmp)
        e = forge.propose("rotto", "mi serve qualcosa che non funzionerà mai bene")
        _write_plugin(tmp, "rotto", BAD_PLUGIN)
        forge.mark_drafted(e.id)
        with pytest.raises(ForgeError):
            forge.test(e.id)
        assert forge.get(e.id).status == "drafted"
        with pytest.raises(ForgeError):
            forge.adopt(e.id)


def test_missing_selftest_blocks():
    with tempfile.TemporaryDirectory() as tmp:
        forge = _make(tmp)
        e = forge.propose("senza_test", "mi serve un tool scritto senza alcuna prova")
        _write_plugin(tmp, "senza_test", NO_SELFTEST_PLUGIN)
        forge.mark_drafted(e.id)
        with pytest.raises(ForgeError):
            forge.test(e.id)


def test_code_change_after_test_requires_retest():
    with tempfile.TemporaryDirectory() as tmp:
        forge = _make(tmp)
        e = forge.propose("mutevole", "mi serve un tool che poi qualcuno modifica")
        _write_plugin(tmp, "mutevole", GOOD_PLUGIN)
        forge.mark_drafted(e.id)
        forge.test(e.id)
        # Il codice cambia DOPO il selftest verde
        _write_plugin(tmp, "mutevole", GOOD_PLUGIN + "\n# modificato\n")
        with pytest.raises(ForgeError):
            forge.adopt(e.id)
        assert forge.get(e.id).status == "drafted"


def test_autonomous_adopt_blocked():
    with tempfile.TemporaryDirectory() as tmp:
        forge = _make(tmp)
        e = forge.propose("notturno", "mi serve un tool forgiato di notte dal heartbeat")
        _write_plugin(tmp, "notturno", GOOD_PLUGIN)
        forge.mark_drafted(e.id)
        forge.test(e.id)
        for source in ("heartbeat", "cron", "subagent"):
            with pytest.raises(ForgeError):
                forge.adopt(e.id, source=source)
        # Con l'owner presente invece si adotta
        assert forge.adopt(e.id, source="cli").status == "adopted"


def test_retire_disables_manifest():
    with tempfile.TemporaryDirectory() as tmp:
        forge = _make(tmp)
        e = forge.propose("pensionato", "mi serve un tool che poi andrà in pensione")
        _write_plugin(tmp, "pensionato", GOOD_PLUGIN)
        forge.mark_drafted(e.id)
        forge.test(e.id)
        forge.adopt(e.id)
        forge.retire(e.id, reason="non serve più")
        manifest_path = os.path.join(tmp, "plugins", "pensionato", "manifest.json")
        with open(manifest_path, encoding="utf-8") as f:
            assert json.load(f)["enabled"] is False
        assert forge.get(e.id).status == "retired"
