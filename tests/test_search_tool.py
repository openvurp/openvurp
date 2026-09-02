from tools.search import _expand_braces, glob_handler


def test_expand_braces_handles_multiple_groups_without_shell():
    assert _expand_braces("{core,tools}/{a.py,b.py}") == [
        "core/a.py", "core/b.py", "tools/a.py", "tools/b.py",
    ]


def test_find_files_accepts_brace_group_and_relative_path_pattern(tmp_path):
    for name in ("HEARTBEAT.md", "SOUL.md", "IDENTITY.md", "USER.md"):
        (tmp_path / name).write_text(name, encoding="utf-8")
    core = tmp_path / "core"
    core.mkdir()
    (core / "agent.py").write_text("", encoding="utf-8")

    grouped = glob_handler(
        "{HEARTBEAT.md,SOUL.md,IDENTITY.md,USER.md}", str(tmp_path),
    )
    nested = glob_handler("core/*.py", str(tmp_path))

    assert grouped.success
    assert "[4 file]" in grouped.output
    assert all(name in grouped.output for name in (
        "HEARTBEAT.md", "SOUL.md", "IDENTITY.md", "USER.md",
    ))
    assert nested.success
    assert "core/agent.py" in nested.output.replace("\\", "/")
