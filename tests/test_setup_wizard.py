"""Test per il setup wizard: logica pura di lettura/scrittura .env."""

import importlib

import core.setup_wizard as sw


def test_parse_env_ignores_comments_and_blanks():
    text = "# commento\n\nLLM_BACKEND=ollama\nLLM_MODEL=\"qwen\"\n"
    env = sw.parse_env(text)
    assert env["LLM_BACKEND"] == "ollama"
    assert env["LLM_MODEL"] == "qwen"  # virgolette rimosse
    assert "#" not in env


def test_apply_env_values_updates_in_place_and_preserves_comments():
    template = (
        "# LLM\n"
        "LLM_BACKEND=ollama\n"
        "LLM_MODEL=old\n"
        "# Telegram\n"
        "TELEGRAM_TOKEN=\n"
    )
    out = sw.apply_env_values(template, {"LLM_MODEL": "new", "TELEGRAM_TOKEN": "abc"})
    assert "LLM_MODEL=new" in out
    assert "TELEGRAM_TOKEN=abc" in out
    assert "LLM_MODEL=old" not in out
    # commenti preservati
    assert "# LLM" in out and "# Telegram" in out


def test_apply_env_values_appends_unknown_keys():
    out = sw.apply_env_values("LLM_BACKEND=ollama\n", {"NEW_KEY": "v"})
    assert "LLM_BACKEND=ollama" in out
    assert "NEW_KEY=v" in out


def test_needs_setup(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    monkeypatch.setattr(sw, "ENV_PATH", env_file)

    # 1. nessun file → serve setup
    assert sw.needs_setup() is True

    # 2. file completo ollama → ok
    env_file.write_text("LLM_BACKEND=ollama\nLLM_MODEL=qwen\n")
    assert sw.needs_setup() is False

    # 3. backend cloud senza chiave → serve setup
    env_file.write_text("LLM_BACKEND=openai\nLLM_MODEL=gpt-4o\n")
    assert sw.needs_setup() is True

    # 4. backend cloud con chiave → ok
    env_file.write_text("LLM_BACKEND=openai\nLLM_MODEL=gpt-4o\nOPENAI_API_KEY=sk-x\n")
    assert sw.needs_setup() is False


def test_write_env_roundtrip(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    example = tmp_path / ".env.example"
    example.write_text("# LLM\nLLM_BACKEND=ollama\nLLM_MODEL=\n")
    monkeypatch.setattr(sw, "ENV_PATH", env_file)
    monkeypatch.setattr(sw, "ENV_EXAMPLE", example)

    sw.write_env({"LLM_BACKEND": "anthropic", "LLM_MODEL": "claude-sonnet-4-6"})
    written = sw.parse_env(env_file.read_text())
    assert written["LLM_BACKEND"] == "anthropic"
    assert written["LLM_MODEL"] == "claude-sonnet-4-6"
    assert sw.needs_setup() is True  # anthropic senza chiave
