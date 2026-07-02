"""Test per il retry Ollama nel client LLM: un hiccup di connessione
(server che riavvia) non deve buttare via il turno; un Timeout invece
non si ritenta (il modello può essere solo lento)."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.llm import LLMClient


class _Resp:
    def raise_for_status(self):
        pass


class FakeRequests:
    """Mimica il modulo requests: fallisce N volte, poi risponde."""

    class exceptions:
        class ConnectionError(Exception):
            pass

        class Timeout(Exception):
            pass

        class HTTPError(Exception):
            pass

    def __init__(self, failures=0, raise_timeout=False):
        self.failures = failures
        self.raise_timeout = raise_timeout
        self.calls = 0

    def post(self, url, json=None, timeout=None, stream=False):
        self.calls += 1
        if self.raise_timeout:
            raise self.exceptions.Timeout("slow")
        if self.calls <= self.failures:
            raise self.exceptions.ConnectionError("refused")
        return _Resp()


def _client(fake) -> LLMClient:
    client = LLMClient.__new__(LLMClient)  # niente init: solo il pezzo che serve
    client._requests = fake
    return client


def test_retries_connection_error_then_succeeds():
    fake = FakeRequests(failures=2)
    client = _client(fake)
    r = client._ollama_post("http://x/api/chat", {}, timeout=5, backoff=0.01)
    assert isinstance(r, _Resp)
    assert fake.calls == 3


def test_raises_after_exhausting_retries():
    fake = FakeRequests(failures=99)
    client = _client(fake)
    try:
        client._ollama_post("http://x/api/chat", {}, timeout=5,
                            retries=2, backoff=0.01)
        assert False, "doveva sollevare ConnectionError"
    except FakeRequests.exceptions.ConnectionError:
        pass
    assert fake.calls == 3


def test_timeout_is_not_retried():
    fake = FakeRequests(raise_timeout=True)
    client = _client(fake)
    try:
        client._ollama_post("http://x/api/chat", {}, timeout=5, backoff=0.01)
        assert False, "doveva sollevare Timeout"
    except FakeRequests.exceptions.Timeout:
        pass
    assert fake.calls == 1


if __name__ == "__main__":
    test_retries_connection_error_then_succeeds()
    test_raises_after_exhausting_retries()
    test_timeout_is_not_retried()
    print("Tutti i test retry LLM passati!")
