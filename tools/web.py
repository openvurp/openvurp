"""
openvurp Tool — Web

HTTP requests e content extraction.
"""

from __future__ import annotations

import re
import time

from core.tools import Tool, ToolResult, ErrorType, RetryPolicy


def web_fetch_handler(url: str, max_chars: int = 10000) -> ToolResult:
    """Fetch URL e estrae testo."""
    try:
        import requests
    except ImportError:
        return ToolResult.fail("requests is not installed. Install it with: pip install requests")

    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (compatible; openvurp/3.0)"
        }
        r = requests.get(url, headers=headers, timeout=30, allow_redirects=True)
        r.raise_for_status()

        content_type = r.headers.get("content-type", "")

        if "text/html" in content_type:
            # Estrai testo da HTML
            text = _html_to_text(r.text)
        elif "application/json" in content_type:
            text = r.text
        else:
            text = r.text

        if len(text) > max_chars:
            text = text[:max_chars] + "\n[...troncato]"

        return ToolResult.ok(f"[{r.status_code}] {url}\n\n{text}")

    except requests.exceptions.Timeout:
        return ToolResult.fail("Timeout", error_type=ErrorType.TIMEOUT, retryable=True)
    except requests.exceptions.ConnectionError:
        return ToolResult.fail("Connection error", error_type=ErrorType.NETWORK, retryable=True)
    except requests.exceptions.HTTPError as e:
        return ToolResult.fail(f"HTTP {e.response.status_code}", error_type=ErrorType.NETWORK)
    except Exception as e:
        return ToolResult.fail(str(e))


def _html_to_text(html: str) -> str:
    """Estrazione testo base da HTML (senza dipendenze extra)."""
    # Drop script and style
    text = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL | re.IGNORECASE)
    # Block tags become newlines
    text = re.sub(r'<(br|p|div|h[1-6]|li|tr)[^>]*>', '\n', text, flags=re.IGNORECASE)
    # Drop the remaining tags
    text = re.sub(r'<[^>]+>', '', text)
    # Decode the basic entities
    text = text.replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>')
    text = text.replace('&quot;', '"').replace('&#39;', "'").replace('&nbsp;', ' ')
    # Squeeze the whitespace
    text = re.sub(r'\n\s*\n', '\n\n', text)
    text = re.sub(r' +', ' ', text)
    return text.strip()


class SearchBlocked(RuntimeError):
    """The engine answered, but not with results (anti-bot page, captcha)."""


# Tried in order until one answers. Measured on a real machine: on any given
# day most of these refuse a bot and exactly one works — but never the same
# one. "auto" rotates internally, the named ones are there for when it does
# not. Depending on a single engine is what made search look broken.
_ENGINES = ("auto", "bing", "duckduckgo", "mojeek", "brave", "startpage", "yahoo")


def _search_library(query: str, limit: int):
    """The maintained library: it handles the token dance the engines require."""
    from ddgs import DDGS

    # A hard ceiling on the whole rotation: seven engines at twelve seconds
    # each would be a minute and a half of an agent standing still.
    deadline = time.monotonic() + 30.0
    refused = []
    for engine in _ENGINES:
        if time.monotonic() > deadline:
            refused.append("out of time")
            break
        try:
            rows = list(DDGS().text(query, backend=engine,
                                    max_results=limit, timeout=12))
        except Exception as exc:
            refused.append(f"{engine}/{type(exc).__name__}")
            continue
        if rows:
            return rows
        refused.append(f"{engine}/empty")
    raise SearchBlocked("every engine refused (" + ", ".join(refused) + ")")


def _unwrap(link: str) -> str:
    if "uddg=" in link:
        import urllib.parse
        parsed = urllib.parse.parse_qs(urllib.parse.urlparse(link).query)
        return parsed.get("uddg", [link])[0]
    return link


def _search_scrape(query: str, limit: int):
    """Fallback with no extra dependency, straight on the HTML endpoint.

    Raises SearchBlocked when the answer is the anti-bot page, so the caller
    can say "blocked" instead of "nothing found".
    """
    import requests

    r = requests.post(
        "https://html.duckduckgo.com/html/",
        data={"q": query},
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                               "AppleWebKit/537.36 (KHTML, like Gecko) "
                               "Chrome/120.0.0.0 Safari/537.36"},
        timeout=15,
    )
    if r.status_code == 202 or "anomaly-modal" in r.text or "captcha" in r.text.lower():
        raise SearchBlocked("the engine answered with an anti-bot page")
    r.raise_for_status()

    rows = []
    pattern = re.compile(
        r'<a[^>]+class="result__a"[^>]*href="([^"]*)"[^>]*>(.*?)</a>', re.DOTALL)
    for match in pattern.finditer(r.text):
        if len(rows) >= limit:
            break
        link = _unwrap(match.group(1))
        title = re.sub(r'<[^>]+>', '', match.group(2)).strip()
        if title and link and not link.startswith("//duckduckgo"):
            rows.append({"title": title, "href": link, "body": ""})
    if not rows:
        raise SearchBlocked("no recognisable result in the answer")
    return rows


def web_search_handler(query: str, max_results: int = 5) -> ToolResult:
    """Searches the web.

    Two lessons are baked in here, both learned the hard way.

    The first: scraping a search engine by hand breaks every time the engine
    changes its markup. So the maintained library goes first, and the hand
    written scraper is only the fallback for when it is unavailable.

    The second matters more, because it was watched live. DuckDuckGo answers a
    bot with an "anomaly" page: HTTP 202, no results, and `raise_for_status()`
    happily passes. The old code read that as "no results found" and the agent
    believed it — it rephrased the same question three times and then gave up,
    convinced the news of the day did not exist. Being blocked and finding
    nothing are opposite facts: one means "try another road", the other means
    "this is not there". They must never come out as the same sentence.
    """
    query = str(query or "").strip()
    if not query:
        return ToolResult.fail("Empty query")
    limit = max(1, min(int(max_results or 5), 20))

    rows, failures = [], []
    for name, search in (("library", _search_library), ("html", _search_scrape)):
        try:
            rows = search(query, limit)
            if rows:
                break
        except ImportError:
            failures.append(f"{name}: not installed")
        except SearchBlocked as exc:
            failures.append(f"{name}: {exc}")
        except Exception as exc:
            failures.append(f"{name}: {type(exc).__name__}")

    if rows:
        out = [f"Results for: {query}"]
        for n, row in enumerate(rows[:limit], 1):
            title = str(row.get("title") or "").strip()
            link = _unwrap(str(row.get("href") or row.get("url") or "").strip())
            body = " ".join(str(row.get("body") or "").split())[:300]
            block = f"[{n}] {title}\n    {link}"
            if body:
                block += f"\n    {body}"
            out.append(block)
        return ToolResult.ok("\n\n".join(out))

    if failures:
        # Deliberately NOT "no results": the search never actually ran. Saying
        # so plainly is what stops the agent from asking the same thing again.
        return ToolResult.fail(
            "The search could not be carried out (" + "; ".join(failures) + "). "
            "This is NOT an empty result: the engine never answered with a list. "
            "Try `web_fetch` on a site you already know, or ask for a link. "
            "Rewording the query will not help.",
            error_type=ErrorType.NETWORK, retryable=True,
        )
    return ToolResult.ok(f"No results found for: {query}")


WEB_SEARCH_TOOL = Tool(
    name="web_search",
    description=(
        "The PRIMARY tool for searching the web. "
        "Use it whenever you need to find pages, news, documentation or how-tos, "
        "or to check a current fact, and you do not already have a URL. "
        "Returns a list of results (title + link + snippet). "
        "To READ a URL you already have, use `web_fetch` instead. "
        "If it comes back saying the search could not be carried out, that is "
        "NOT an empty result: the engine blocked us. Change road — do not ask "
        "the same question in different words."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "What to search for"},
            "max_results": {"type": "integer", "description": "Max results (default: 5)"}
        },
        "required": ["query"]
    },
    timeout=40,
    retry_policy=RetryPolicy(max_retries=1, retryable_errors=[ErrorType.TIMEOUT, ErrorType.NETWORK]),
    handler=web_search_handler
)


WEB_FETCH_TOOL = Tool(
    name="web_fetch",
    description=(
        "Downloads one URL and extracts its text (HTML to text, JSON raw). "
        "Use it after `web_search` to read one of the results, or when you are "
        "given a link directly. Do not use it to search: with no URL, start from `web_search`."
    ),
    parameters={
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "URL to download"},
            "max_chars": {"type": "integer", "description": "Max output characters (default: 10000)"}
        },
        "required": ["url"]
    },
    timeout=30,
    retry_policy=RetryPolicy(max_retries=1, retryable_errors=[ErrorType.TIMEOUT, ErrorType.NETWORK]),
    handler=web_fetch_handler
)
