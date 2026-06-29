"""
openvurp Tools — Browser

Tool browser di alto livello sopra BrowserManager.

- `mode="shared"`: browser reale dell'utente via CDP.
- `mode="isolated"`: browser Playwright separato.
- `mode="auto"`: decide il manager.
"""

from __future__ import annotations

from core.browser_manager import get_browser_manager
from core.tools import Tool, ToolResult, ErrorType, RetryPolicy


def browser_handler(
    action: str,
    mode: str = "auto",
    engine: str = "",
    channel: str = "",
    headless: bool | None = None,
    url: str = "",
    selector: str = "",
    text: str = "",
    js: str = "",
    path: str = "",
    index: int = 0,
    timeout_ms: int = 4000,
) -> ToolResult:
    manager = get_browser_manager()
    try:
        if action == "status":
            return ToolResult.ok(manager.status())
        if action == "connect":
            return ToolResult.ok(manager.connect(mode=mode, url=url, engine=engine, channel=channel, headless=headless))
        if action == "relaunch":
            return ToolResult.ok(manager.relaunch(url=url))
        if action == "list_pages":
            return ToolResult.ok(manager.list_pages(mode=mode, engine=engine))
        if action == "select_page":
            return ToolResult.ok(manager.select_page(index=index, mode=mode, engine=engine))
        if action == "navigate":
            return ToolResult.ok(manager.navigate(url=url, mode=mode, engine=engine, channel=channel, headless=headless))
        if action == "new_page":
            return ToolResult.ok(manager.new_page(url=url, mode=mode, engine=engine, channel=channel, headless=headless))
        if action in {"read", "get_text"}:
            return ToolResult.ok(manager.read(mode=mode, engine=engine, channel=channel, headless=headless))
        if action == "click":
            return ToolResult.ok(manager.click(selector=selector, mode=mode, engine=engine, channel=channel, headless=headless))
        if action == "fill":
            return ToolResult.ok(manager.fill(selector=selector, text=text, mode=mode, engine=engine, channel=channel, headless=headless))
        if action == "type":
            return ToolResult.ok(manager.type_text(selector=selector, text=text, mode=mode, engine=engine, channel=channel, headless=headless))
        if action == "wait":
            return ToolResult.ok(manager.wait(selector=selector, timeout_ms=timeout_ms, mode=mode, engine=engine, channel=channel, headless=headless))
        if action == "evaluate":
            return ToolResult.ok(manager.evaluate(js=js, mode=mode, engine=engine, channel=channel, headless=headless))
        if action == "screenshot":
            return ToolResult.ok(manager.screenshot(path=path, mode=mode, engine=engine, channel=channel, headless=headless))
        if action == "close":
            return ToolResult.ok(manager.close(mode=mode, engine=engine))
        return ToolResult.fail(
            "Azione sconosciuta. Azioni: status, connect, relaunch, list_pages, "
            "select_page, navigate, new_page, read, click, fill, type, wait, "
            "evaluate, screenshot, close",
            error_type=ErrorType.VALIDATION,
        )
    except RuntimeError as exc:
        message = str(exc)
        error_type = ErrorType.DEPENDENCY if "install" in message.lower() or "playwright" in message.lower() else ErrorType.RUNTIME
        return ToolResult.fail(message, error_type=error_type)
    except Exception as exc:
        return ToolResult.fail(f"Browser error: {exc}", error_type=ErrorType.RUNTIME)


BROWSER_TOOL = Tool(
    name="browser",
    description=(
        "Controllo browser ad alto livello. "
        "`mode=shared` usa Chrome reale dell'utente via CDP; "
        "`mode=isolated` usa un browser Playwright separato con engine chromium/firefox/webkit; "
        "per Chromium supporta anche canali branded come chrome, chrome-beta, chrome-dev, chrome-canary, "
        "msedge, msedge-beta, msedge-dev e msedge-canary; "
        "`mode=auto` sceglie il backend adatto. "
        "Usalo per status, navigate, read, click, fill, screenshot e gestione tab."
    ),
    parameters={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "description": (
                    "Azione: status, connect, relaunch, list_pages, select_page, "
                    "navigate, new_page, read, click, fill, type, wait, evaluate, screenshot, close"
                ),
            },
            "mode": {
                "type": "string",
                "description": "Backend desiderato: auto, shared, isolated",
            },
            "engine": {
                "type": "string",
                "description": "Engine Playwright per mode=isolated: chromium, firefox, webkit",
            },
            "channel": {
                "type": "string",
                "description": (
                    "Canale Chromium per mode=isolated: chromium, chrome, chrome-beta, "
                    "chrome-dev, chrome-canary, msedge, msedge-beta, msedge-dev, msedge-canary"
                ),
            },
            "headless": {
                "type": "boolean",
                "description": "Solo per mode=isolated: avvia il browser senza UI",
            },
            "url": {"type": "string", "description": "URL per navigate/connect/new_page/relaunch"},
            "selector": {"type": "string", "description": "CSS selector per click/fill/type/wait"},
            "text": {"type": "string", "description": "Testo per fill/type"},
            "js": {"type": "string", "description": "JavaScript per evaluate"},
            "path": {"type": "string", "description": "Percorso file per screenshot"},
            "index": {"type": "integer", "description": "Indice pagina per select_page"},
            "timeout_ms": {"type": "integer", "description": "Timeout per wait in millisecondi"},
        },
        "required": ["action"],
    },
    requires_approval=True,
    handler=browser_handler,
    timeout=60,
    retry_policy=RetryPolicy(max_retries=1, retryable_errors=[ErrorType.TIMEOUT, ErrorType.NETWORK]),
)
