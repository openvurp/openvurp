"""Tests for audio/voice disable switches."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.tools import ErrorType
from tools.media import audio_transcribe_handler
from tools.voice_tools import listen_mic_handler, speak_handler


def test_speak_tool_respects_voice_enabled_switch():
    import config

    original = getattr(config, "VOICE_ENABLED", False)
    config.VOICE_ENABLED = False
    try:
        result = speak_handler("hello", play=False)
    finally:
        config.VOICE_ENABLED = original

    assert not result.success
    assert result.error_type == ErrorType.PERMISSION
    assert "VOICE_ENABLED" in (result.error or "")


def test_listen_mic_tool_respects_mic_enabled_switch():
    import config

    original = getattr(config, "MIC_ENABLED", False)
    config.MIC_ENABLED = False
    try:
        result = listen_mic_handler(duration=0.1)
    finally:
        config.MIC_ENABLED = original

    assert not result.success
    assert result.error_type == ErrorType.PERMISSION
    assert "MIC_ENABLED" in (result.error or "")


def test_audio_transcribe_respects_audio_switch_before_file_check():
    import config

    original_audio = getattr(config, "AUDIO_ENABLED", True)
    original_transcribe = getattr(config, "AUDIO_TRANSCRIBE_ENABLED", True)
    config.AUDIO_ENABLED = False
    config.AUDIO_TRANSCRIBE_ENABLED = False
    try:
        result = audio_transcribe_handler("/tmp/does-not-exist.mp3")
    finally:
        config.AUDIO_ENABLED = original_audio
        config.AUDIO_TRANSCRIBE_ENABLED = original_transcribe

    assert not result.success
    assert result.error_type == ErrorType.PERMISSION
    assert "AUDIO_ENABLED" in (result.error or "")


if __name__ == "__main__":
    test_speak_tool_respects_voice_enabled_switch()
    test_listen_mic_tool_respects_mic_enabled_switch()
    test_audio_transcribe_respects_audio_switch_before_file_check()
    print("Voice config tests passed.")
