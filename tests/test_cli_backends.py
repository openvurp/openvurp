import json
import os
import queue
import subprocess
from unittest.mock import patch

import pytest

from core.cli_backends import (
    CLIRunResult,
    ClaudeCLIBackend,
    CodexCLIBackend,
    split_context,
    compact_messages,
)
from core.llm import create_llm_client
from core.llm import LLMError


def test_compact_messages_keeps_identity_constraints_and_latest_turn():
    system = "IDENTITA-INIZIO\n" + ("x" * 20000) + "\nVINCOLI-FINE"
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": "vecchio" * 5000},
        {"role": "assistant", "content": "risposta vecchia"},
        {"role": "user", "content": "DOMANDA-RECENTE"},
    ]
    prompt = compact_messages(messages, max_chars=8000)
    assert "IDENTITA-INIZIO" in prompt
    assert "VINCOLI-FINE" in prompt
    assert "DOMANDA-RECENTE" in prompt
    assert len(prompt) <= 8200


def test_codex_backend_uses_chatgpt_login_without_api_key():
    output = "\n".join([
        json.dumps({"type": "thread.started", "thread_id": "thread_1"}),
        json.dumps({
            "type": "item.completed",
            "item": {"type": "agent_message", "text": "risposta Codex"},
        }),
        json.dumps({
            "type": "turn.completed",
            "usage": {"input_tokens": 123, "output_tokens": 9},
        }),
    ])
    seen = {}

    def fake_run(command, **kwargs):
        seen.update(command=command, **kwargs)
        return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")

    backend = CodexCLIBackend(
        binary="codex", model="gpt-5.6-luna", workspace=".",
        sandbox="read-only", require_subscription_login=True,
    )
    with patch("core.cli_backends.shutil.which", return_value="/bin/codex"), \
            patch("core.cli_backends.codex_login_status", return_value=(True, "ChatGPT")), \
            patch("core.cli_backends.subprocess.run", side_effect=fake_run), \
            patch.dict(os.environ, {"OPENAI_API_KEY": "NON_DEVE_PASSARE"}):
        result = backend.run([{"role": "user", "content": "ciao"}])

    assert result == CLIRunResult(
        text="risposta Codex", input_tokens=123, output_tokens=9,
        raw={"type": "turn.completed", "usage": {"input_tokens": 123, "output_tokens": 9}},
    )
    assert "--ephemeral" in seen["command"]
    assert seen["env"].get("OPENAI_API_KEY") is None


def test_codex_app_server_streams_real_deltas_and_ignores_commentary():
    requests = []
    output = queue.Queue()

    class FakeStdout:
        def readline(self):
            return output.get(timeout=2)

    class FakeStderr:
        def readline(self):
            return ""

    class FakeStdin:
        def write(self, raw):
            request = json.loads(raw)
            requests.append(request)
            method = request.get("method")
            if method == "initialize":
                output.put(json.dumps({"id": 1, "result": {}}) + "\n")
            elif method == "thread/start":
                output.put(json.dumps({
                    "id": 2, "result": {"thread": {"id": "thread-live"}},
                }) + "\n")
            elif method == "turn/start":
                scripted = [
                    {"id": 3, "result": {"turn": {"id": "turn-live"}}},
                    {"method": "item/started", "params": {
                        "item": {"id": "tool-live", "type": "dynamicToolCall",
                                 "tool": "web_search", "arguments": {"query": "Taranto"},
                                 "status": "inProgress"},
                    }},
                    # Collisioni intenzionali con gli id delle richieste
                    # thread/start (2) e initialize (1): in JSON-RPC
                    # bidirezionale sono legittime e non sono risposte.
                    {"id": 2, "method": "item/tool/call", "params": {
                        "callId": "call-live", "threadId": "thread-live",
                        "turnId": "turn-live", "tool": "web_search",
                        "arguments": {"query": "Taranto"},
                    }},
                    {"method": "item/completed", "params": {
                        "item": {"id": "tool-live", "type": "dynamicToolCall",
                                 "tool": "web_search", "status": "completed",
                                 "success": True},
                    }},
                    {"method": "item/started", "params": {
                        "item": {"id": "tool-live-2", "type": "dynamicToolCall",
                                 "tool": "web_search", "arguments": {"query": "Puglia"},
                                 "status": "inProgress"},
                    }},
                    {"id": 1, "method": "item/tool/call", "params": {
                        "callId": "call-live-2", "threadId": "thread-live",
                        "turnId": "turn-live", "tool": "web_search",
                        "arguments": {"query": "Puglia"},
                    }},
                    {"method": "item/completed", "params": {
                        "item": {"id": "tool-live-2", "type": "dynamicToolCall",
                                 "tool": "web_search", "status": "completed",
                                 "success": True},
                    }},
                    {"method": "item/started", "params": {
                        "item": {"id": "note", "type": "agentMessage",
                                 "phase": "commentary"},
                    }},
                    {"method": "item/agentMessage/delta", "params": {
                        "itemId": "note", "delta": "testo interno",
                    }},
                    {"method": "item/started", "params": {
                        "item": {"id": "answer", "type": "agentMessage",
                                 "phase": "final_answer"},
                    }},
                    {"method": "item/agentMessage/delta", "params": {
                        "itemId": "answer", "delta": "Ciao ",
                    }},
                    {"method": "item/agentMessage/delta", "params": {
                        "itemId": "answer", "delta": "in diretta",
                    }},
                    {"method": "item/completed", "params": {
                        "item": {"id": "answer", "type": "agentMessage",
                                 "phase": "final_answer", "text": "Ciao in diretta"},
                    }},
                    {"method": "thread/tokenUsage/updated", "params": {
                        "tokenUsage": {"last": {
                            "inputTokens": 44, "outputTokens": 8,
                        }},
                    }},
                    {"method": "turn/completed", "params": {
                        "turn": {"id": "turn-live", "status": "completed"},
                    }},
                ]
                for event in scripted:
                    output.put(json.dumps(event) + "\n")
            return len(raw)

        def flush(self):
            pass

        def close(self):
            output.put("")

    class FakeProcess:
        def __init__(self):
            self.stdin = FakeStdin()
            self.stdout = FakeStdout()
            self.stderr = FakeStderr()
            self.returncode = None

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = 0

        def kill(self):
            self.returncode = -9

        def wait(self, timeout=None):
            return self.returncode

    backend = CodexCLIBackend(
        binary="codex", model="gpt-5.6-luna", workspace=".",
        sandbox="read-only", require_subscription_login=True,
    )
    deltas = []
    tool_calls = []
    events = []
    with patch("core.cli_backends.shutil.which", return_value="/bin/codex"), \
            patch("core.cli_backends.codex_login_status", return_value=(True, "ChatGPT")), \
            patch("core.cli_backends.subprocess.Popen", return_value=FakeProcess()):
        result = backend.run_stream(
            [{"role": "user", "content": "ciao"}],
            on_text=deltas.append, on_event=events.append,
            tools_schema=[{
                "type": "function",
                "function": {
                    "name": "web_search", "description": "Cerca con Vurp",
                    "parameters": {
                        "type": "object", "properties": {
                            "query": {"type": "string"},
                        }, "required": ["query"],
                    },
                },
            }],
            on_tool=lambda name, args: tool_calls.append((name, args)) or "trovato",
        )

    assert deltas == ["Ciao ", "in diretta"]
    assert result.text == "Ciao in diretta"
    assert (result.input_tokens, result.output_tokens) == (44, 8)
    assert [request.get("method") for request in requests[:4]] == [
        "initialize", "initialized", "thread/start", "turn/start",
    ]
    thread_params = requests[2]["params"]
    assert thread_params["ephemeral"] is True
    assert thread_params["approvalPolicy"] == "never"
    assert thread_params["dynamicTools"][0]["name"] == "web_search"
    assert requests[0]["params"]["capabilities"]["experimentalApi"] is True
    assert tool_calls == [
        ("web_search", {"query": "Taranto"}),
        ("web_search", {"query": "Puglia"}),
    ]
    tool_responses = [
        request for request in requests
        if request.get("id") in {1, 2} and "method" not in request
    ]
    assert len(tool_responses) == 2
    assert tool_responses[0]["result"] == {
        "contentItems": [{"type": "inputText", "text": "trovato"}],
        "success": True,
    }
    assert any(event.get("method") == "item/started" for event in events)


def test_claude_cli_backend_uses_subscription_without_api_key():
    payload = {
        "type": "result", "subtype": "success", "is_error": False,
        "result": "risposta Claude",
        "usage": {"input_tokens": 80, "cache_read_input_tokens": 20,
                  "output_tokens": 7},
    }
    seen = {}

    def fake_run(command, **kwargs):
        seen.update(command=command, **kwargs)
        return subprocess.CompletedProcess(
            command, 0, stdout=json.dumps(payload), stderr="",
        )

    backend = ClaudeCLIBackend(
        binary="claude", model="sonnet", workspace=".",
        require_subscription_login=True,
    )
    with patch("core.cli_backends.shutil.which", return_value="/bin/claude"), \
            patch("core.cli_backends.claude_login_status", return_value=(True, "Claude.ai")), \
            patch("core.cli_backends.subprocess.run", side_effect=fake_run), \
            patch.dict(os.environ, {"ANTHROPIC_API_KEY": "NON_DEVE_PASSARE"}):
        result = backend.run([{"role": "user", "content": "ciao"}])

    assert result.text == "risposta Claude"
    assert (result.input_tokens, result.output_tokens) == (100, 7)
    assert "--safe-mode" in seen["command"]
    assert seen["env"].get("ANTHROPIC_API_KEY") is None


def test_llm_factory_has_low_cost_codex_default_and_real_usage():
    fake = type("FakeBackend", (), {
        "run": lambda self, messages: CLIRunResult("ok", 321, 12),
    })()
    with patch("core.cli_backends.CodexCLIBackend", return_value=fake):
        client = create_llm_client("codex")
        text, _duration, input_tokens, output_tokens = client.call_with_timing([
            {"role": "user", "content": "test"},
        ])
    assert client.model == "gpt-5.6-luna"
    assert text == "ok"
    assert (input_tokens, output_tokens) == (321, 12)


def test_llm_codex_streaming_forwards_deltas_and_real_usage():
    class FakeBackend:
        def run_stream(self, messages, on_text=None, on_event=None,
                       tools_schema=None, on_tool=None):
            on_text("uno ")
            on_text("due")
            return CLIRunResult("uno due", 70, 5)

    with patch("core.cli_backends.CodexCLIBackend", return_value=FakeBackend()):
        client = create_llm_client("codex")
        deltas = []
        text, _duration, input_tokens, output_tokens = \
            client.call_streamed_with_timing(
                [{"role": "user", "content": "test"}], on_text=deltas.append,
            )

    assert client.supports_text_streaming
    assert client.supports_tool_transport
    assert deltas == ["uno ", "due"]
    assert text == "uno due"
    assert (input_tokens, output_tokens) == (70, 5)


def test_codex_does_not_fallback_after_openvurp_tool_execution():
    class FakeBackend:
        fallback_called = False

        def run_stream(self, messages, on_text=None, on_event=None,
                       tools_schema=None, on_tool=None):
            on_tool("web_search", {"query": "test"})
            raise RuntimeError("stream interrotto dopo il tool")

        def run(self, messages):
            self.fallback_called = True
            return CLIRunResult("duplicato")

    fake = FakeBackend()
    with patch("core.cli_backends.CodexCLIBackend", return_value=fake):
        client = create_llm_client("codex")
        calls = []
        with pytest.raises(LLMError, match="stream interrotto"):
            client.call_streamed(
                [{"role": "user", "content": "test"}],
                tools_schema=[{"type": "function", "function": {
                    "name": "web_search", "description": "cerca",
                    "parameters": {"type": "object"},
                }}],
                on_tool=lambda name, args: calls.append((name, args)) or "ok",
            )
    assert calls == [("web_search", {"query": "test"})]
    assert fake.fallback_called is False


def test_full_system_prompt_survives_a_realistic_window():
    """Regressione: il system prompt veniva tagliato nel mezzo.

    Con il vecchio default (12.000 caratteri, meta' dei quali al system prompt)
    identita', metodo operativo e indice dei tool sparivano: l'agente sembrava
    aver dimenticato i propri file .md.
    """
    system = "IDENTITA\n" + ("regola operativa importante\n" * 1200) + "METODO-FINALE"
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": "DOMANDA"},
    ]
    prompt = compact_messages(messages, max_chars=180000)
    assert system in prompt
    assert "[...contesto compattato...]" not in prompt
    assert "DOMANDA" in prompt


def test_system_prompt_never_starves_the_recent_turn():
    """Anche con un system enorme, l'ultima domanda deve entrare."""
    messages = [
        {"role": "system", "content": "S" * 200000},
        {"role": "user", "content": "DOMANDA-RECENTE"},
    ]
    prompt = compact_messages(messages, max_chars=20000)
    assert "DOMANDA-RECENTE" in prompt
    assert len(prompt) <= 20500


def test_split_context_separates_system_from_conversation():
    """Regressione: l'identita' finiva dentro il turno utente.

    I CLI agentici hanno uno slot di sistema vero. Se il system prompt viaggia
    come testo del messaggio utente, a governare resta il prompt base del CLI
    ("sei un coding agent") e l'agente non si riconosce nei propri file .md.
    """
    messages = [
        {"role": "system", "content": "SONO-OPENVURP e seguo i miei file .md"},
        {"role": "user", "content": "chi sei?"},
    ]
    system, conversation = split_context(messages, max_chars=180000)

    assert "SONO-OPENVURP" in system
    assert "SONO-OPENVURP" not in conversation
    assert "chi sei?" in conversation
    # Il preambolo deve dire esplicitamente che queste istruzioni vincono
    # sull'identita' predefinita del CLI.
    assert "coding agent" in system


def test_split_context_without_system_returns_only_the_conversation():
    system, conversation = split_context([{"role": "user", "content": "ciao"}])
    assert system == ""
    assert "ciao" in conversation
