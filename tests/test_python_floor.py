"""The project must actually run on the oldest Python it claims.

Found by CI, not here: `pyproject.toml` says `requires-python = ">=3.10"` and
the README badge says 3.10+, but `agent.py` had six f-strings with a backslash
inside the expression part — `f"{'\\u2500' * pad}"`. That is legal from 3.12
onwards and a SyntaxError before it. Not a failing test: the file could not be
read at all, so on 3.10 and 3.11 nothing started.

The local interpreter is newer, which is exactly why nobody noticed. So this
does not compile — it reads the sources and looks for the two constructs that
a newer interpreter accepts silently.
"""

import ast
import re
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def declared_floor() -> tuple[int, int]:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    raw = data["project"]["requires-python"]
    found = re.search(r"(\d+)\.(\d+)", raw)
    assert found, f"unreadable requires-python: {raw!r}"
    return int(found.group(1)), int(found.group(2))


def sources() -> list[Path]:
    out = subprocess.run(["git", "ls-files", "*.py"], cwd=ROOT,
                         capture_output=True, text=True, check=True)
    return [ROOT / line for line in out.stdout.split() if line]


def test_the_floor_is_declared_and_sane():
    major, minor = declared_floor()
    assert major == 3 and minor >= 10


def test_no_backslash_inside_an_f_string_expression():
    """The exact break CI caught. Only valid from 3.12."""
    if declared_floor() >= (3, 12):
        return                      # then it is allowed; nothing to guard
    opener = re.compile(r"""(?:^|[^\w'"])f(['"])""")
    guilty = []
    for path in sources():
        for number, line in enumerate(path.read_text(encoding="utf-8",
                                                     errors="ignore").splitlines(), 1):
            for start in opener.finditer(line):
                for slot in re.finditer(r"\{([^{}]*)\}", line[start.end():]):
                    if "\\" in slot.group(1):
                        guilty.append(f"{path.relative_to(ROOT)}:{number}")
                        break
    assert not guilty, (
        "backslash inside an f-string expression — SyntaxError below 3.12, so "
        "these files do not even load: " + ", ".join(sorted(set(guilty))))


def test_every_source_parses_at_the_declared_floor():
    """Catches the grammar the floor does not have (match, walrus in odd spots…).

    It does not catch the f-string case above — `feature_version` does not
    restore that restriction — which is why the two tests are separate.
    """
    floor = declared_floor()
    broken = []
    for path in sources():
        text = path.read_text(encoding="utf-8", errors="ignore")
        try:
            ast.parse(text, feature_version=floor)
        except SyntaxError as exc:
            broken.append(f"{path.relative_to(ROOT)}:{exc.lineno} {exc.msg}")
    assert not broken, "not valid on the declared floor: " + "; ".join(broken)


def test_the_readme_badge_says_the_same_floor():
    """A badge promising a version that does not work is worse than no badge."""
    major, minor = declared_floor()
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert f"python-{major}.{minor}" in readme.replace("%2B", "+"), (
        f"the badge does not say {major}.{minor}, which is what pyproject requires")


def test_the_local_interpreter_is_not_the_proof():
    """A reminder in code: passing here does not mean 3.10 was tried."""
    assert sys.version_info >= declared_floor()
