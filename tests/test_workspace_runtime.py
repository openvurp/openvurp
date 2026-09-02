"""Test per bootstrap legacy, self-evolution e memoria sessioni."""

import os
import sys
import tempfile
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.bootstrap import BootstrapLoader, normalize_workspace_filename, resolve_workspace_file
from core.executor import Executor
from core.memory import MemoryManager
from core.personality import (
    SILENCE_TOKEN,
    format_callback_response,
    parse_response_directive,
    prepare_outbound_response,
    slack_reaction_name,
)
from core.session import Session
from core.tools import ToolRegistry
from main import finalize_channel_response, read_identity_name
from tools import evolve as evolve_tools
from tools import media as media_tools


def test_bootstrap_loader_reads_legacy_lowercase_soul():
    with tempfile.TemporaryDirectory() as tmp:
        with open(os.path.join(tmp, "soul.md"), "w", encoding="utf-8") as f:
            f.write("Sono openvurp.\n")
        with open(os.path.join(tmp, "AGENTS.md"), "w", encoding="utf-8") as f:
            f.write("Regole.\n")

        loader = BootstrapLoader(tmp)
        files = loader.load_all()
        loaded = {entry.name: entry for entry in files if not entry.missing}

        assert "SOUL.md" in loaded
        assert loaded["SOUL.md"].content == "Sono openvurp.\n"
        assert loaded["SOUL.md"].path.endswith("soul.md")

        context = loader.build_project_context(files)
        assert "SOUL.md è presente" in context
        assert "Sono openvurp." in context


def test_workspace_file_resolution_normalizes_legacy_names():
    with tempfile.TemporaryDirectory() as tmp:
        with open(os.path.join(tmp, "soul.md"), "w", encoding="utf-8") as f:
            f.write("ciao")

        canonical, path = resolve_workspace_file(tmp, "SOUL.md")
        assert canonical == "SOUL.md"
        assert path.endswith("soul.md")
        assert normalize_workspace_filename("soul.md") == "SOUL.md"


def test_bootstrap_main_loads_daily_memory_but_group_skips_private_memory():
    with tempfile.TemporaryDirectory() as tmp:
        os.makedirs(os.path.join(tmp, "memory"), exist_ok=True)
        for name in ("AGENTS.md", "SOUL.md", "USER.md", "IDENTITY.md", "TOOLS.md", "MEMORY.md"):
            with open(os.path.join(tmp, name), "w", encoding="utf-8") as f:
                f.write(name)

        today = datetime.now().date().isoformat()
        yesterday = (datetime.now().date() - timedelta(days=1)).isoformat()
        with open(os.path.join(tmp, "memory", f"{today}.md"), "w", encoding="utf-8") as f:
            f.write("oggi")
        with open(os.path.join(tmp, "memory", f"{yesterday}.md"), "w", encoding="utf-8") as f:
            f.write("ieri")

        loader = BootstrapLoader(tmp)
        main_files = {entry.name for entry in loader.load_all(session_type="main") if not entry.missing}
        group_files = {entry.name for entry in loader.load_all(session_type="group") if not entry.missing}

        assert "MEMORY.md" in main_files
        assert f"memory/{today}.md" in main_files
        assert f"memory/{yesterday}.md" in main_files
        assert "MEMORY.md" not in group_files
        assert not any(name.startswith("memory/") for name in group_files)


def test_bootstrap_context_marks_missing_workspace_files():
    with tempfile.TemporaryDirectory() as tmp:
        with open(os.path.join(tmp, "AGENTS.md"), "w", encoding="utf-8") as f:
            f.write("regole")

        loader = BootstrapLoader(tmp)
        context = loader.build_project_context(loader.load_all())

        assert "File workspace mancante" in context
        assert "SOUL.md" in context


def test_evolve_tools_follow_the_real_workspace_file():
    original_get_openvurp_dir = evolve_tools._get_openvurp_dir

    with tempfile.TemporaryDirectory() as tmp:
        with open(os.path.join(tmp, "soul.md"), "w", encoding="utf-8") as f:
            f.write("vecchio")
        evolve_tools._get_openvurp_dir = lambda: tmp
        try:
            result = evolve_tools._evolve_handler(
                file="SOUL.md",
                content="nuovo contenuto",
                reason="test",
            )
            assert "[EVOLUZIONE]" in result
            with open(os.path.join(tmp, "soul.md"), "r", encoding="utf-8") as f:
                assert f.read() == "nuovo contenuto"

            read_back = evolve_tools._read_self_handler(file="SOUL.md")
            assert read_back == "nuovo contenuto"
        finally:
            evolve_tools._get_openvurp_dir = original_get_openvurp_dir


def test_memory_manager_can_recall_recent_session_previews():
    with tempfile.TemporaryDirectory() as tmp:
        session_dir = os.path.join(tmp, "sessions")
        session = Session(session_dir=session_dir)
        session.add_message("user", "Parlavamo della fattura di marzo per il cliente ACME.")
        session.add_message("assistant", "Ti avevo ricordato di inviarla entro venerdi.")
        session.save()

        memory = MemoryManager(tmp)
        relevant = memory.get_relevant("Mi ricordi la fattura ACME?", budget_chars=2000)

        assert "memory/sessions/" in relevant
        assert "fattura" in relevant.lower()
        assert "acme" in relevant.lower()


def test_memory_manager_skips_dynamic_memory_outside_private_main_session():
    with tempfile.TemporaryDirectory() as tmp:
        os.makedirs(os.path.join(tmp, "sessions"), exist_ok=True)
        with open(os.path.join(tmp, "profilo.json"), "w", encoding="utf-8") as f:
            f.write('{"nome": "Mario"}')
        today = datetime.now().date().isoformat()
        with open(os.path.join(tmp, f"{today}.md"), "w", encoding="utf-8") as f:
            f.write("Promessa aperta sul deploy.")

        memory = MemoryManager(tmp)
        assert memory.get_relevant("deploy", session_type="group") == "(nessun ricordo ancora)"


def test_channel_response_suppresses_explicit_silence_token():
    assert prepare_outbound_response(f"  {SILENCE_TOKEN}  ", source="telegram") == ""
    assert finalize_channel_response(f"\n{SILENCE_TOKEN}\n", "telegram") == ""
    assert finalize_channel_response("ci sono", "telegram") == "ci sono"


def test_reaction_token_is_parsed_and_preserved_for_supported_channels():
    directive = parse_response_directive("[[react:👍]]")
    assert directive.kind == "reaction"
    assert directive.emoji == "👍"
    assert format_callback_response("[[react:👍]]", "telegram") == "[[react:👍]]"
    assert prepare_outbound_response("[[react:👍]]", "telegram") == ""
    assert slack_reaction_name("👍") == "thumbsup"


def test_read_identity_name_uses_identity_md_without_framework_name():
    with tempfile.TemporaryDirectory() as tmp:
        identity_path = os.path.join(tmp, "IDENTITY.md")
        with open(identity_path, "w", encoding="utf-8") as f:
            f.write("# IDENTITY.md\n\n- **Nome:** Otto\n")

        def _load(path: str) -> str:
            with open(path, "r", encoding="utf-8") as handle:
                return handle.read()

        assert read_identity_name(tmp, _load) == "Otto"


def test_read_identity_name_accepts_english_identity_md():
    with tempfile.TemporaryDirectory() as tmp:
        identity_path = os.path.join(tmp, "IDENTITY.md")
        with open(identity_path, "w", encoding="utf-8") as f:
            f.write("# IDENTITY.md\n\n- **Name:** openvurp\n")

        def _load(path: str) -> str:
            with open(path, "r", encoding="utf-8") as handle:
                return handle.read()

        assert read_identity_name(tmp, _load) == "openvurp"


def test_image_analyze_uses_dedicated_vision_model():
    import config
    import requests

    captured = {}

    class DummyResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"message": {"content": "ok"}}

    original_backend = getattr(config, "LLM_BACKEND", None)
    original_llm_model = getattr(config, "LLM_MODEL", None)
    original_vision_model = getattr(config, "VISION_MODEL", None)
    original_base_url = getattr(config, "LLM_BASE_URL", None)
    original_post = requests.post

    with tempfile.TemporaryDirectory() as tmp:
        image_path = os.path.join(tmp, "sample.png")
        with open(image_path, "wb") as f:
            f.write(b"fake-image")

        def fake_post(url, json=None, timeout=None):
            captured["url"] = url
            captured["json"] = json
            captured["timeout"] = timeout
            return DummyResponse()

        config.LLM_BACKEND = "ollama"
        config.LLM_MODEL = "glm-5.1:cloud"
        config.VISION_MODEL = "qwen3-vl:235b-instruct-cloud"
        config.LLM_BASE_URL = "http://ollama.local"
        requests.post = fake_post

        try:
            result = media_tools.image_analyze_handler(image_path, prompt="descrivi")
        finally:
            requests.post = original_post
            config.LLM_BACKEND = original_backend
            config.LLM_MODEL = original_llm_model
            config.VISION_MODEL = original_vision_model
            config.LLM_BASE_URL = original_base_url

    assert result.success
    assert captured["url"] == "http://ollama.local/api/chat"
    assert captured["json"]["model"] == "qwen3-vl:235b-instruct-cloud"


def test_image_analyze_does_not_reuse_codex_chat_backend():
    """La visione deve funzionare anche quando la chat usa un backend CLI."""
    import config
    import requests

    captured = {}

    class DummyResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"message": {"content": "vista"}}

    original = {
        name: getattr(config, name, None)
        for name in ("LLM_BACKEND", "LLM_MODEL", "VISION_BACKEND",
                     "VISION_MODEL", "LLM_BASE_URL")
    }
    original_post = requests.post

    with tempfile.TemporaryDirectory() as tmp:
        image_path = os.path.join(tmp, "photo.jpg")
        with open(image_path, "wb") as f:
            f.write(b"fake-image")

        def fake_post(url, json=None, timeout=None):
            captured["url"] = url
            captured["json"] = json
            captured["timeout"] = timeout
            return DummyResponse()

        config.LLM_BACKEND = "codex"
        config.LLM_MODEL = "gpt-5.6-luna"
        config.VISION_BACKEND = "ollama"
        config.VISION_MODEL = "qwen3-vl:235b-instruct-cloud"
        config.LLM_BASE_URL = "http://vision.local"
        requests.post = fake_post

        try:
            result = media_tools.image_analyze_handler(image_path, prompt="cosa vedi?")
        finally:
            requests.post = original_post
            for name, value in original.items():
                setattr(config, name, value)

    assert result.success
    assert captured["url"] == "http://vision.local/api/chat"
    assert captured["json"]["model"] == "qwen3-vl:235b-instruct-cloud"


if __name__ == "__main__":
    test_bootstrap_loader_reads_legacy_lowercase_soul()
    test_workspace_file_resolution_normalizes_legacy_names()
    test_bootstrap_main_loads_daily_memory_but_group_skips_private_memory()
    test_bootstrap_context_marks_missing_workspace_files()
    test_evolve_tools_follow_the_real_workspace_file()
    test_memory_manager_can_recall_recent_session_previews()
    test_channel_response_suppresses_explicit_silence_token()
    test_reaction_token_is_parsed_and_preserved_for_supported_channels()
    test_read_identity_name_uses_identity_md_without_framework_name()
    test_read_identity_name_accepts_english_identity_md()
    test_image_analyze_uses_dedicated_vision_model()
    print("Tutti i test workspace/runtime passati!")
