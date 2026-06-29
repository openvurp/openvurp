"""
openvurp Tool — Web

HTTP requests e content extraction.
"""

from __future__ import annotations

import re

from core.tools import Tool, ToolResult, ErrorType, RetryPolicy


def web_fetch_handler(url: str, max_chars: int = 10000) -> ToolResult:
    """Fetch URL e estrae testo."""
    try:
        import requests
    except ImportError:
        return ToolResult.fail("requests non installato. Installa con: pip install requests")

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
    # Rimuovi script e style
    text = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL | re.IGNORECASE)
    # Blocchi → newline
    text = re.sub(r'<(br|p|div|h[1-6]|li|tr)[^>]*>', '\n', text, flags=re.IGNORECASE)
    # Rimuovi tag
    text = re.sub(r'<[^>]+>', '', text)
    # Decode entities base
    text = text.replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>')
    text = text.replace('&quot;', '"').replace('&#39;', "'").replace('&nbsp;', ' ')
    # Comprimi whitespace
    text = re.sub(r'\n\s*\n', '\n\n', text)
    text = re.sub(r' +', ' ', text)
    return text.strip()


def web_search_handler(query: str, max_results: int = 5) -> ToolResult:
    """Cerca sul web usando DuckDuckGo HTML."""
    try:
        import requests
    except ImportError:
        return ToolResult.fail("requests non installato. Installa con: pip install requests")

    try:
        url = "https://html.duckduckgo.com/html/"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        r = requests.post(url, data={"q": query}, headers=headers, timeout=15)
        r.raise_for_status()

        # Parse risultati dal HTML
        results = []
        # Pattern per estrarre risultati DuckDuckGo
        result_pattern = re.compile(
            r'<a[^>]+class="result__a"[^>]*href="([^"]*)"[^>]*>(.*?)</a>'
            r'.*?<a[^>]+class="result__snippet"[^>]*>(.*?)</a>',
            re.DOTALL
        )

        for match in result_pattern.finditer(r.text):
            if len(results) >= max_results:
                break
            link = match.group(1)
            # DuckDuckGo wrappa i link in redirect
            if "uddg=" in link:
                import urllib.parse
                parsed = urllib.parse.parse_qs(urllib.parse.urlparse(link).query)
                link = parsed.get("uddg", [link])[0]
            title = re.sub(r'<[^>]+>', '', match.group(2)).strip()
            snippet = re.sub(r'<[^>]+>', '', match.group(3)).strip()
            if title and link:
                results.append(f"[{len(results)+1}] {title}\n    {link}\n    {snippet}")

        if not results:
            # Fallback: pattern più semplice
            simple_pattern = re.compile(
                r'<a[^>]+class="result__a"[^>]*href="([^"]*)"[^>]*>(.*?)</a>',
                re.DOTALL
            )
            for match in simple_pattern.finditer(r.text):
                if len(results) >= max_results:
                    break
                link = match.group(1)
                if "uddg=" in link:
                    import urllib.parse
                    parsed = urllib.parse.parse_qs(urllib.parse.urlparse(link).query)
                    link = parsed.get("uddg", [link])[0]
                title = re.sub(r'<[^>]+>', '', match.group(2)).strip()
                if title and link and not link.startswith("//duckduckgo"):
                    results.append(f"[{len(results)+1}] {title}\n    {link}")

        if results:
            output = f"Risultati per: {query}\n\n" + "\n\n".join(results)
            return ToolResult.ok(output)
        else:
            return ToolResult.ok(f"No results found for: {query}")

    except requests.exceptions.Timeout:
        return ToolResult.fail("Timeout ricerca", error_type=ErrorType.TIMEOUT, retryable=True)
    except requests.exceptions.ConnectionError:
        return ToolResult.fail("Connection error", error_type=ErrorType.NETWORK, retryable=True)
    except Exception as e:
        return ToolResult.fail(f"Search error: {e}")


WEB_SEARCH_TOOL = Tool(
    name="web_search",
    description=(
        "Tool PRIMARIO per cercare informazioni sul web (DuckDuckGo). "
        "Usalo ogni volta che ti serve trovare pagine, notizie, documentazione, "
        "how-to o verificare fatti aggiornati e non hai gia' un URL specifico. "
        "Ritorna una lista di risultati (titolo + link + snippet). "
        "Per LEGGERE un URL gia' noto usa invece `web_fetch`. "
        "NON usare `browser_devtools` per ricerche: quello serve a debug di Chrome."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Query di ricerca"},
            "max_results": {"type": "integer", "description": "Numero max risultati (default: 5)"}
        },
        "required": ["query"]
    },
    timeout=20,
    retry_policy=RetryPolicy(max_retries=1, retryable_errors=[ErrorType.TIMEOUT, ErrorType.NETWORK]),
    handler=web_search_handler
)


WEB_FETCH_TOOL = Tool(
    name="web_fetch",
    description=(
        "Scarica un URL specifico ed estrae il testo (HTML → testo, JSON → raw). "
        "Usalo dopo `web_search` per leggere uno dei risultati, o quando l'utente "
        "ti passa un link diretto. NON usarlo per cercare: se non hai un URL, parti da `web_search`."
    ),
    parameters={
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "URL da scaricare"},
            "max_chars": {"type": "integer", "description": "Max caratteri output (default: 10000)"}
        },
        "required": ["url"]
    },
    timeout=30,
    retry_policy=RetryPolicy(max_retries=1, retryable_errors=[ErrorType.TIMEOUT, ErrorType.NETWORK]),
    handler=web_fetch_handler
)
