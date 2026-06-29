"""Test per il report capability derivato dal wiring reale."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.capabilities import inspect_runtime_capabilities, render_capability_prompt


def test_capability_report_detects_config_without_tools():
    import config

    original_vision_model = getattr(config, "VISION_MODEL", None)
    original_audio_model = getattr(config, "AUDIO_MODEL", None)

    config.VISION_MODEL = "qwen3-vl:235b-instruct-cloud"
    config.AUDIO_MODEL = "base"
    try:
        report = inspect_runtime_capabilities([])
    finally:
        config.VISION_MODEL = original_vision_model
        config.AUDIO_MODEL = original_audio_model

    assert any("image_analyze" in warning for warning in report.warnings)
    assert any("audio_transcribe" in warning for warning in report.warnings)


def test_capability_prompt_mentions_non_automatic_media_flow():
    report = inspect_runtime_capabilities(["image_analyze", "audio_transcribe", "pdf_read"])
    text = render_capability_prompt(report)

    assert "image_analyze" in text
    assert "audio_transcribe" in text
    assert "pdf_read" in text
    assert "non riceve automaticamente immagini o audio raw" in text
    assert "non fingerla" in text


def test_capability_prompt_mentions_desktop_and_outbound_media_tools():
    report = inspect_runtime_capabilities(
        ["image_analyze", "desktop_screenshot", "notify_file", "notify_photo"]
    )
    text = render_capability_prompt(report)

    assert "desktop_screenshot" in text
    assert "notify_file" in text
    assert "notify_photo" in text


def test_capability_prompt_mentions_browser_devtools_when_present():
    report = inspect_runtime_capabilities(["browser_devtools"])
    text = render_capability_prompt(report)

    assert "browser_devtools" in text
    assert "Chrome" in text
    assert "Debugging avanzato" in text


def test_capability_prompt_mentions_browser_tool_when_present():
    report = inspect_runtime_capabilities(["browser", "browser_devtools"])
    text = render_capability_prompt(report)

    assert "browser" in text
    assert "mode=\"shared\"" in text
    assert "mode=\"isolated\"" in text
    assert "action=\"relaunch\"" in text
    assert "firefox" in text
    assert "webkit" in text


def test_audio_and_voice_can_be_disabled_by_config():
    import config

    originals = {
        "AUDIO_ENABLED": getattr(config, "AUDIO_ENABLED", True),
        "AUDIO_TRANSCRIBE_ENABLED": getattr(config, "AUDIO_TRANSCRIBE_ENABLED", True),
        "VOICE_ENABLED": getattr(config, "VOICE_ENABLED", False),
        "VOICE_TOOLS_ENABLED": getattr(config, "VOICE_TOOLS_ENABLED", False),
        "MIC_ENABLED": getattr(config, "MIC_ENABLED", False),
    }
    config.AUDIO_ENABLED = False
    config.AUDIO_TRANSCRIBE_ENABLED = False
    config.VOICE_ENABLED = False
    config.VOICE_TOOLS_ENABLED = False
    config.MIC_ENABLED = False
    try:
        report = inspect_runtime_capabilities(["audio_transcribe", "speak", "listen_mic"])
        text = render_capability_prompt(report)
    finally:
        for key, value in originals.items():
            setattr(config, key, value)

    assert not report.audio_file_tool
    assert not report.tts_tool
    assert not report.microphone_tool
    assert "Audio e vocali da file: disattivati" in text
    assert "Voce in uscita: disattivata" in text
    assert "Microfono live: disattivato" in text


if __name__ == "__main__":
    test_capability_report_detects_config_without_tools()
    test_capability_prompt_mentions_non_automatic_media_flow()
    test_capability_prompt_mentions_desktop_and_outbound_media_tools()
    test_capability_prompt_mentions_browser_devtools_when_present()
    test_capability_prompt_mentions_browser_tool_when_present()
    test_audio_and_voice_can_be_disabled_by_config()
    print("Tutti i test capabilities passati!")
