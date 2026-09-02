"""Memory across sessions, channel replies, and the vision model.

This file used to also cover the workspace files openvurp read about
itself, and the tools that rewrote them. openvurp is the platform: it
has no identity file, so those went with the files.
"""

import os
import sys
import tempfile
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.memory import MemoryManager
from core.personality import (
    SILENCE_TOKEN,
    format_callback_response,
    parse_response_directive,
    prepare_outbound_response,
    slack_reaction_name,
)
from core.session import Session
from main import finalize_channel_response
from tools import media as media_tools


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
    test_memory_manager_can_recall_recent_session_previews()
    test_channel_response_suppresses_explicit_silence_token()
    test_reaction_token_is_parsed_and_preserved_for_supported_channels()
    test_image_analyze_uses_dedicated_vision_model()
    print("Tutti i test workspace/runtime passati!")
