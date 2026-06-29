"""I file tool restano nel workspace quando il sandbox è attivo."""

import config
from tools.file_ops import read_file_handler, _check_sandbox


def test_read_outside_workspace_blocked(monkeypatch):
    monkeypatch.setattr(config, "SANDBOX_MODE", "restricted")
    monkeypatch.setattr(config, "SANDBOX_ALLOWED_PATHS", [])
    r = read_file_handler("/etc/passwd")
    assert r.success is False
    assert "workspace" in (r.error or "").lower()


def test_read_inside_workspace_ok(monkeypatch):
    monkeypatch.setattr(config, "SANDBOX_MODE", "restricted")
    r = read_file_handler("README.md")
    assert r.success is True


def test_sandbox_off_allows_outside(monkeypatch):
    monkeypatch.setattr(config, "SANDBOX_MODE", "off")
    assert _check_sandbox("/etc/passwd") is None


def test_allowed_paths_extra(monkeypatch, tmp_path):
    extra = tmp_path / "data"
    extra.mkdir()
    monkeypatch.setattr(config, "SANDBOX_MODE", "restricted")
    monkeypatch.setattr(config, "SANDBOX_ALLOWED_PATHS", [str(extra)])
    assert _check_sandbox(str(extra / "x.txt")) is None       # consentito
    assert _check_sandbox("/etc/passwd") is not None           # ancora bloccato
