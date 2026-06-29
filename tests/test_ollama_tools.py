"""Test per il tool calling nativo di Ollama."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.llm import LLMClient


class DummyHTTPError(Exception):
    def __init__(self, response=None):
        super().__init__("http error")
        self.response = response


class DummyTimeout(Exception):
    pass


class DummyConnectionError(Exception):
    pass


class DummyResponse:
    def __init__(self, data=None, status_code=200, raise_http=False):
        self._data = data or {}
        self.status_code = status_code
        self._raise_http = raise_http

    def raise_for_status(self):
        if self._raise_http:
            raise DummyHTTPError(response=self)

    def json(self):
        return self._data


class DummyRequests:
    class exceptions:
        Timeout = DummyTimeout
        ConnectionError = DummyConnectionError
        HTTPError = DummyHTTPError

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def post(self, url, json=None, timeout=None, stream=False):
        self.calls.append({
            "url": url,
            "json": json,
            "timeout": timeout,
            "stream": stream,
        })
        if not self._responses:
            raise AssertionError("Nessuna response fake disponibile")
        return self._responses.pop(0)


def make_client() -> LLMClient:
    return LLMClient(backend="ollama", model="qwen3", base_url="http://ollama.local")


def test_to_ollama_messages_supports_tool_history():
    client = make_client()
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "leggi"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "tc1", "name": "read_file", "args": {"path": "/tmp/x.txt"}},
            ],
        },
        {
            "role": "tool_result",
            "tool_call_id": "tc1",
            "name": "read_file",
            "content": "contenuto",
        },
    ]

    converted = client._to_ollama_messages(messages)

    assert converted[0]["role"] == "system"
    assert converted[2]["tool_calls"][0]["function"]["name"] == "read_file"
    assert converted[2]["tool_calls"][0]["function"]["arguments"]["path"] == "/tmp/x.txt"
    assert converted[3]["role"] == "tool"
    assert converted[3]["tool_name"] == "read_file"
    assert converted[3]["content"] == "contenuto"


def test_ollama_native_tool_calls_are_parsed():
    client = make_client()
    dummy = DummyRequests([
        DummyResponse({
            "message": {
                "content": "",
                "tool_calls": [
                    {
                        "function": {
                            "name": "read_file",
                            "arguments": {"path": "/tmp/config.py"},
                        }
                    }
                ],
            },
            "done_reason": "tool_calls",
        })
    ])
    client._requests = dummy

    tools_schema = [{
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Legge un file",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
        }
    }]

    response = client.call_with_tools(
        [{"role": "user", "content": "leggi config.py"}],
        tools_schema,
    )

    assert dummy.calls[0]["json"]["tools"] == tools_schema
    assert len(response.tool_calls) == 1
    assert response.tool_calls[0].name == "read_file"
    assert response.tool_calls[0].args["path"] == "/tmp/config.py"
    assert client.supports_function_calling


def test_ollama_disables_native_tools_when_server_rejects_them():
    client = make_client()
    dummy = DummyRequests([
        DummyResponse(status_code=400, raise_http=True),
        DummyResponse({
            "message": {
                "content": "```TOOL:read_file\n{\"path\": \"/tmp/x.txt\"}\n```"
            }
        }),
    ])
    client._requests = dummy

    response = client.call_with_tools(
        [{"role": "user", "content": "leggi /tmp/x.txt"}],
        [{
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Legge un file",
                "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
            }
        }],
    )

    assert len(dummy.calls) == 2
    assert "tools" in dummy.calls[0]["json"]
    assert "tools" not in dummy.calls[1]["json"]
    assert "read_file" in response.text
    assert not client.supports_function_calling


if __name__ == "__main__":
    test_to_ollama_messages_supports_tool_history()
    test_ollama_native_tool_calls_are_parsed()
    test_ollama_disables_native_tools_when_server_rejects_them()
    print("Tutti i test ollama tools passati!")
