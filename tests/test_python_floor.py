"""The project must actually run on the oldest Python it claims.

Found by CI, not here: pyproject says `requires-python = ">=3.10"` and the
README badge says 3.10+, but six f-strings in `agent.py` had a backslash inside
the expression part. That is legal from 3.12 onwards and a SyntaxError before
it — not a failing test, a file that could not be read, so on 3.10 and 3.11
nothing started at all.

The local interpreter is newer, which is exactly why nobody noticed.

Two things this file learned the hard way about testing a floor you are not
standing on. It must not use anything newer than that floor itself — the first
version imported `tomllib`, which arrives in 3.11, so the guard broke the
version it guarded. And it walks the syntax tree rather than matching text: a
regex looking for backslashes near an f-string flagged this very docstring for
describing the bug.
"""

import ast
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def declared_floor() -> tuple:
    """Read requires-python without tomllib: that module is 3.11+."""
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    found = re.search(r"""requires-python\s*=\s*["'][^"']*?(\d+)\.(\d+)""", text)
    assert found, "requires-python not found in pyproject.toml"
    return int(found.group(1)), int(found.group(2))


def sources() -> list:
    """Tracked sources. Anything untracked is not what people clone."""
    done = subprocess.run(["git", "ls-files", "*.py"], cwd=str(ROOT),
                          capture_output=True, text=True, check=True)
    return [ROOT / line for line in done.stdout.split() if line]


def _backslashes_in_f_string_expressions(path: Path) -> list:
    """Line numbers where an f-string expression contains a backslash.

    Walking the tree instead of the text is the point: a docstring that merely
    talks about the construct is not the construct.
    """
    text = path.read_text(encoding="utf-8", errors="ignore")
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        return [exc.lineno or 0]
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.JoinedStr):
            continue
        for piece in node.values:
            if not isinstance(piece, ast.FormattedValue):
                continue
            segment = ast.get_source_segment(text, piece.value) or ""
            if "\\" in segment:
                hits.append(piece.lineno)
    return hits


def test_the_floor_is_declared_and_sane():
    major, minor = declared_floor()
    assert (major, minor) >= (3, 10)


def test_no_backslash_inside_an_f_string_expression():
    """The exact break CI caught. Only valid from 3.12."""
    if declared_floor() >= (3, 12):
        return                      # then it is allowed; nothing to guard
    guilty = []
    for path in sources():
        for line in _backslashes_in_f_string_expressions(path):
            guilty.append(f"{path.relative_to(ROOT)}:{line}")
    assert not guilty, (
        "backslash inside an f-string expression — SyntaxError below 3.12, so "
        "these files do not even load: " + ", ".join(sorted(set(guilty))))


def test_this_guard_uses_nothing_newer_than_the_floor():
    """A guard that needs 3.11 to check 3.10 is worse than no guard.

    tomllib was the first mistake here and it cost a red CI run; the list is
    the standard-library modules that arrived after the floor.
    """
    too_new = {(3, 11): {"tomllib"}, (3, 12): {"typing_extensions"}}
    forbidden = set()
    for version, names in too_new.items():
        if version > declared_floor():
            forbidden |= names
    text = Path(__file__).read_text(encoding="utf-8")
    tree = ast.parse(text)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    clash = imported & forbidden
    assert not clash, f"this file imports {clash}, unavailable on the declared floor"


def test_every_source_parses_at_the_declared_floor():
    """Grammar the floor does not have (match, newer soft keywords…).

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
    assert "python-{}.{}".format(major, minor) in readme.replace("%2B", "+"), (
        "the badge does not say {}.{}, which is what pyproject requires".format(
            major, minor))


def test_the_local_interpreter_is_not_the_proof():
    """A reminder in code: passing here does not mean the floor was tried."""
    assert sys.version_info[:2] >= declared_floor()
