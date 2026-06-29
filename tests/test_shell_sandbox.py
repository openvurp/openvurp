"""Test per il tool shell con sandbox reale."""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config as cfg

from core.tools import ErrorType
from tools.shell import shell_handler


def test_shell_handler_uses_sandbox_allowed_paths():
    original_mode = getattr(cfg, "SANDBOX_MODE", "restricted")
    original_paths = list(getattr(cfg, "SANDBOX_ALLOWED_PATHS", []))
    original_shell = getattr(cfg, "SHELL", "")

    allowed_dir = tempfile.mkdtemp(prefix="openvurp-shell-allowed-")
    blocked_dir = tempfile.mkdtemp(prefix="openvurp-shell-blocked-")

    try:
        cfg.SANDBOX_MODE = "restricted"
        cfg.SANDBOX_ALLOWED_PATHS = [allowed_dir]
        cfg.SHELL = "/bin/bash"

        ok = shell_handler("pwd", workdir=allowed_dir)
        assert ok.success
        assert allowed_dir in ok.output

        blocked = shell_handler("pwd", workdir=blocked_dir)
        assert not blocked.success
        assert blocked.error_type == ErrorType.PERMISSION
        assert "Path fuori dal sandbox" in (blocked.error or "")
    finally:
        cfg.SANDBOX_MODE = original_mode
        cfg.SANDBOX_ALLOWED_PATHS = original_paths
        cfg.SHELL = original_shell


if __name__ == "__main__":
    test_shell_handler_uses_sandbox_allowed_paths()
    print("Tutti i test shell sandbox passati!")
