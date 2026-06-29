"""Test per screenshot desktop."""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tools.desktop as desktop_tools


def test_desktop_screenshot_handler_uses_powershell_backend_when_available():
    with tempfile.TemporaryDirectory() as tmp:
        target = os.path.join(tmp, "screen.png")
        original_power = desktop_tools._capture_with_powershell
        original_pillow = desktop_tools._capture_with_pillow
        original_which = desktop_tools.shutil.which

        def fake_power(path: str):
            with open(path, "wb") as f:
                f.write(b"png")

        def fake_pillow(path: str):
            raise AssertionError("Pillow backend non dovrebbe essere usato")

        desktop_tools._capture_with_powershell = fake_power
        desktop_tools._capture_with_pillow = fake_pillow
        desktop_tools.shutil.which = lambda name: "/usr/bin/powershell.exe" if name == "powershell.exe" else None
        try:
            result = desktop_tools.desktop_screenshot_handler(path=target)
        finally:
            desktop_tools._capture_with_powershell = original_power
            desktop_tools._capture_with_pillow = original_pillow
            desktop_tools.shutil.which = original_which

        assert result.success
        assert os.path.exists(target)


if __name__ == "__main__":
    test_desktop_screenshot_handler_uses_powershell_backend_when_available()
    print("Tutti i test desktop tool passati!")
