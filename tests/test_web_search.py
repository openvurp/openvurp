"""Being blocked and finding nothing are opposite facts.

Watched live: an agent was asked for the news of the day, the search tool
answered "No results found" three times, and it concluded the news did not
exist. It had never searched at all — DuckDuckGo had returned its anti-bot
page (HTTP 202) and the old code read that as an empty result. The agent kept
rewording the same question because the answer it got said the question was
the problem.

So: whatever else changes here, an engine that refuses us must never come out
sounding like an engine that answered "nothing".
"""

import pytest

import tools.web as W


class _Answer:
    def __init__(self, status, text):
        self.status_code, self.text = status, text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


def _no_library(monkeypatch):
    monkeypatch.setattr(W, "_search_library",
                        lambda *_a, **_k: (_ for _ in ()).throw(ImportError()))


def test_the_anti_bot_page_is_not_reported_as_an_empty_result(monkeypatch):
    _no_library(monkeypatch)
    import requests
    monkeypatch.setattr(requests, "post", lambda *a, **k: _Answer(
        202, "<div class='anomaly-modal__box'>unusual traffic</div>"))

    result = W.web_search_handler("news of the day", 3)

    assert not result.success, "a blocked search must not pass for a good one"
    said = (result.error or "") + (result.output or "")
    assert "No results found" not in said
    assert "NOT an empty result" in said
    # And it has to say what to do instead, or the agent just tries again.
    assert "web_fetch" in said and "will not help" in said


def test_a_captcha_counts_as_blocked_too(monkeypatch):
    _no_library(monkeypatch)
    import requests
    monkeypatch.setattr(requests, "post", lambda *a, **k: _Answer(
        200, "<html><body>Please solve the CAPTCHA to continue</body></html>"))

    assert not W.web_search_handler("anything", 3).success


def test_a_genuinely_empty_answer_still_says_no_results(monkeypatch):
    """The other half: when the engine really answers nothing, say so."""
    monkeypatch.setattr(W, "_search_library", lambda *_a, **_k: [])
    monkeypatch.setattr(W, "_search_scrape", lambda *_a, **_k: [])

    result = W.web_search_handler("kjhgfdsaqwertzuiop", 3)
    assert result.success
    assert "No results found" in result.output


def test_results_come_back_titled_and_linked(monkeypatch):
    monkeypatch.setattr(W, "_search_library", lambda *_a, **_k: [
        {"title": "Shareholder Letters", "href": "https://example.com/a",
         "body": "the letters"},
    ])
    result = W.web_search_handler("letters", 3)
    assert result.success
    assert "Shareholder Letters" in result.output
    assert "https://example.com/a" in result.output
    assert "\n\n\n" not in result.output, "blank line left over in the header"


def test_one_engine_refusing_does_not_end_the_search(monkeypatch):
    """The real fix: rotate. On any given day only one engine answers."""
    seen = []

    class _Engine:
        def text(self, _query, backend=None, **_kw):
            seen.append(backend)
            if backend != "bing":
                raise RuntimeError("refused")
            return [{"title": "found", "href": "https://x", "body": ""}]

    monkeypatch.setattr(W, "_ENGINES", ("auto", "bing", "mojeek"))
    monkeypatch.setitem(__import__("sys").modules, "ddgs",
                        type("m", (), {"DDGS": _Engine})())

    rows = W._search_library("q", 3)
    assert rows and rows[0]["title"] == "found"
    assert seen[0] == "auto", "the rotating backend should be tried first"
    assert "bing" in seen


def test_when_every_engine_refuses_it_is_blocked_not_empty(monkeypatch):
    class _Engine:
        def text(self, *_a, **_k):
            raise RuntimeError("refused")

    monkeypatch.setattr(W, "_ENGINES", ("auto", "bing"))
    monkeypatch.setitem(__import__("sys").modules, "ddgs",
                        type("m", (), {"DDGS": _Engine})())

    with pytest.raises(W.SearchBlocked):
        W._search_library("q", 3)


def test_the_tool_description_warns_about_the_trap():
    """The agent reads this text, not this test file."""
    described = W.WEB_SEARCH_TOOL.description
    assert "NOT an empty result" in described
    assert "same question in different words" in described
