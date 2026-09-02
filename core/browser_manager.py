"""
openvurp Core — Browser Manager

Layer unificato per il controllo browser.

- `shared`: riusa un browser Chromium reale tramite CDP, utile per sessioni già loggate.
- `isolated`: usa un browser Playwright separato e deterministico.

Il manager evita discovery shell ripetuta e concentra routing, preflight e fallback in un solo punto.
"""

from __future__ import annotations

import importlib.util
import json
import os
import platform
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Optional

import config
from tools.browser_devtools import ChromeDevToolsMCP, BROWSER_PORT


OPENVURP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CAPTURES_DIR = os.path.join(OPENVURP_DIR, "memory", "captures")
PROBE_CACHE_DIR = os.path.join(OPENVURP_DIR, "memory", "cache")
PROBE_CACHE_PATH = os.path.join(PROBE_CACHE_DIR, "browser_probe.json")


def _runtime_platform_tag() -> str:
    system = platform.system().lower()
    machine = platform.machine().lower()
    if any(token in machine for token in ("aarch64", "arm64")):
        arch = "arm64"
    elif "arm" in machine:
        arch = "arm"
    else:
        arch = "x64"
    if system.startswith("win"):
        family = "windows"
    elif system == "darwin":
        family = "macos"
    else:
        family = "linux"
    return f"{family}-{arch}"


PLAYWRIGHT_RUNTIME_TAG = _runtime_platform_tag()
PLAYWRIGHT_RUNTIME_ROOT = os.path.join(OPENVURP_DIR, "memory", "runtime", "playwright")
PLAYWRIGHT_VENDOR_DIR = os.path.join(PLAYWRIGHT_RUNTIME_ROOT, PLAYWRIGHT_RUNTIME_TAG, "site")
PLAYWRIGHT_BROWSERS_DIR = os.path.join(PLAYWRIGHT_RUNTIME_ROOT, PLAYWRIGHT_RUNTIME_TAG, "browsers")
LEGACY_PLAYWRIGHT_VENDOR_DIR = os.path.join(OPENVURP_DIR, "memory", "runtime", "playwright_site")
LEGACY_PLAYWRIGHT_BROWSERS_DIR = os.path.join(OPENVURP_DIR, "memory", "runtime", "playwright_browsers")
PLAYWRIGHT_PROBE_TTL_SECONDS = 600
PLAYWRIGHT_PROBE_TIMEOUT_SECONDS = 3.0
PLAYWRIGHT_INSTALL_MSG = (
    "Playwright non pronto nel workspace. Usa `browser_setup` oppure prepara manualmente:\n"
    f"  python -m pip install --target '{PLAYWRIGHT_VENDOR_DIR}' playwright\n"
    f"  PYTHONPATH='{PLAYWRIGHT_VENDOR_DIR}' PLAYWRIGHT_BROWSERS_PATH='{PLAYWRIGHT_BROWSERS_DIR}' python -m playwright install chromium"
)
SUPPORTED_ENGINES = ("chromium", "firefox", "webkit")
SUPPORTED_CHANNELS = (
    "",
    "chromium",
    "chrome",
    "chrome-beta",
    "chrome-dev",
    "chrome-canary",
    "msedge",
    "msedge-beta",
    "msedge-dev",
    "msedge-canary",
)


def choose_browser_mode(
    requested_mode: str,
    action: str,
    remote_debugging: bool,
    browser_running: bool,
    engine: str = "chromium",
) -> str:
    normalized = (requested_mode or "auto").strip().lower()
    if normalized in {"shared", "isolated"}:
        return normalized
    if (engine or "chromium").strip().lower() != "chromium":
        return "isolated"

    shared_first_actions = {
        "status",
        "connect",
        "relaunch",
        "list_pages",
        "select_page",
        "snapshot",
        "console",
        "network",
    }
    if action in shared_first_actions:
        return "shared"
    if remote_debugging or browser_running:
        return "shared"
    return "isolated"


@dataclass
class BrowserStatus:
    remote_debugging: bool
    browser_running: bool
    browser_executable: str
    playwright_available: bool
    playwright_driver_ready: bool
    shared_ready: bool
    isolated_ready: bool
    default_engine: str
    supported_engines: tuple[str, ...]
    engine_states: dict[str, str]
    browsers_dir: str
    last_error: str = ""

    def render(self) -> str:
        engine_status = ", ".join(
            f"{name}={self.engine_states.get(name, 'unknown')}" for name in self.supported_engines
        )
        lines = [
            f"remote_debugging: {'on' if self.remote_debugging else 'off'}",
            f"browser_running: {'yes' if self.browser_running else 'no'}",
            f"browser_executable: {self.browser_executable or '(not found)'}",
            f"playwright: {'available' if self.playwright_available else 'missing'}",
            f"playwright_driver: {'ready' if self.playwright_driver_ready else 'not ready'}",
            f"playwright_vendor_dir: {PLAYWRIGHT_VENDOR_DIR}",
            f"playwright_browsers_dir: {self.browsers_dir}",
            f"shared_mode: {'ready' if self.shared_ready else 'not ready'}",
            f"isolated_mode: {'ready' if self.isolated_ready else 'not ready'}",
            f"default_engine: {self.default_engine}",
            f"isolated_engines: {engine_status}",
            f"chromium_channels: {', '.join(channel for channel in SUPPORTED_CHANNELS if channel)}",
        ]
        if self.browser_running and not self.remote_debugging:
            lines.append(
                "next_step: abilita chrome://inspect/#remote-debugging oppure usa action=relaunch con approvazione"
            )
        elif not self.playwright_driver_ready and self.playwright_available:
            lines.append("next_step: esegui browser_setup oppure doctor_fix per riparare il runtime Playwright")
        elif not self.browser_running and self.playwright_driver_ready:
            lines.append("next_step: auto puo usare mode=isolated per pagine pubbliche")
        if self.last_error:
            lines.append(f"last_error: {self.last_error}")
        return "\n".join(lines)


class BrowserManager:
    def __init__(self):
        self._devtools = ChromeDevToolsMCP(auto_connect=False)
        self._playwright_runtime = None
        self._shared_browser = None
        self._shared_context = None
        self._isolated_browser = None
        self._isolated_context = None
        self._isolated_page = None
        self._isolated_engine = ""
        self._isolated_channel = ""
        self._isolated_headless = True
        self._shared_page_index = -1
        self._last_error = ""
        self._probe_cache: Optional[dict] = None

    def _legacy_runtime_supported(self) -> bool:
        return platform.system() == "Linux"

    def _vendor_dir(self) -> str:
        if os.path.isdir(PLAYWRIGHT_VENDOR_DIR):
            return PLAYWRIGHT_VENDOR_DIR
        if self._legacy_runtime_supported() and os.path.isdir(LEGACY_PLAYWRIGHT_VENDOR_DIR):
            return LEGACY_PLAYWRIGHT_VENDOR_DIR
        return PLAYWRIGHT_VENDOR_DIR

    def _normalize_engine(self, engine: str = "") -> str:
        selected = (engine or getattr(config, "BROWSER_DEFAULT_ENGINE", "chromium") or "chromium").strip().lower()
        if selected not in SUPPORTED_ENGINES:
            raise RuntimeError(f"Engine browser non supportato: {selected}")
        return selected

    def _normalize_channel(self, channel: str = "") -> str:
        selected = (channel or getattr(config, "BROWSER_DEFAULT_CHANNEL", "") or "").strip().lower()
        if selected not in SUPPORTED_CHANNELS:
            raise RuntimeError(f"Channel browser non supportato: {selected}")
        return selected

    def _normalize_headless(self, headless: Optional[bool]) -> bool:
        if headless is None:
            return bool(getattr(config, "BROWSER_DEFAULT_HEADLESS", True))
        return bool(headless)

    def _normalize_mode(self, mode: str = "") -> str:
        selected = (mode or getattr(config, "BROWSER_DEFAULT_MODE", "auto") or "auto").strip().lower()
        if selected not in {"auto", "shared", "isolated"}:
            raise RuntimeError(f"Mode browser non supportato: {selected}")
        return selected

    def _playwright_available(self) -> bool:
        self._ensure_playwright_vendor_path()
        try:
            return importlib.util.find_spec("playwright.sync_api") is not None
        except ModuleNotFoundError:
            return False

    def _ensure_playwright_vendor_path(self) -> None:
        vendor_dir = self._vendor_dir()
        if os.path.isdir(vendor_dir) and vendor_dir not in sys.path:
            sys.path.insert(0, vendor_dir)

    def _effective_browsers_dir(self) -> str:
        explicit = str(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "") or "").strip()
        if explicit:
            return explicit
        if os.path.isdir(PLAYWRIGHT_BROWSERS_DIR) and os.listdir(PLAYWRIGHT_BROWSERS_DIR):
            return PLAYWRIGHT_BROWSERS_DIR
        if self._legacy_runtime_supported() and os.path.isdir(LEGACY_PLAYWRIGHT_BROWSERS_DIR) and os.listdir(LEGACY_PLAYWRIGHT_BROWSERS_DIR):
            return LEGACY_PLAYWRIGHT_BROWSERS_DIR
        return os.path.expanduser("~/.cache/ms-playwright")

    def _load_probe_cache(self) -> Optional[dict]:
        if self._probe_cache:
            return self._probe_cache
        try:
            with open(PROBE_CACHE_PATH, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict):
            return None
        self._probe_cache = data
        return data

    def _store_probe_cache(self, data: dict) -> None:
        os.makedirs(PROBE_CACHE_DIR, exist_ok=True)
        try:
            with open(PROBE_CACHE_PATH, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2)
        except OSError:
            return
        self._probe_cache = data

    def _run_playwright_probe(
        self,
        mode: str,
        engine: str = "",
        timeout_seconds: float = PLAYWRIGHT_PROBE_TIMEOUT_SECONDS,
    ) -> tuple[bool, str]:
        script = r"""
import json
import os
import sys

vendor_dir = sys.argv[1]
browsers_dir = sys.argv[2]
mode = sys.argv[3]
engine = sys.argv[4] if len(sys.argv) > 4 else ""

if vendor_dir and vendor_dir not in sys.path:
    sys.path.insert(0, vendor_dir)
if browsers_dir:
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = browsers_dir

try:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        if mode == "driver":
            print(json.dumps({"ok": True, "detail": "driver ready"}))
        else:
            browser = getattr(pw, engine).launch(headless=True)
            try:
                page = browser.new_page()
                page.goto("data:text/html,<title>probe</title><h1>probe</h1>", wait_until="domcontentloaded")
                print(json.dumps({"ok": True, "detail": page.title() or "ok"}))
            finally:
                browser.close()
except Exception as exc:
    print(json.dumps({"ok": False, "detail": str(exc)}))
    raise SystemExit(1)
"""
        env = os.environ.copy()
        vendor_dir = self._vendor_dir()
        pythonpath = vendor_dir
        if env.get("PYTHONPATH"):
            pythonpath = pythonpath + os.pathsep + env["PYTHONPATH"]
        env["PYTHONPATH"] = pythonpath
        env["PLAYWRIGHT_BROWSERS_PATH"] = self._effective_browsers_dir()
        command = [
            sys.executable,
            "-c",
            script,
            vendor_dir,
            env["PLAYWRIGHT_BROWSERS_PATH"],
            mode,
            engine,
        ]
        try:
            result = subprocess.run(
                command,
                cwd=OPENVURP_DIR,
                env=env,
                capture_output=True,
                text=True,
                timeout=max(2.0, timeout_seconds),
                check=False,
            )
        except subprocess.TimeoutExpired:
            return False, f"timeout oltre {int(max(2.0, timeout_seconds))}s"
        except Exception as exc:
            return False, str(exc)

        combined = ((result.stdout or "") + "\n" + (result.stderr or "")).strip()
        detail = combined
        payload = None
        for line in reversed(combined.splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
                break
            except json.JSONDecodeError:
                continue
        if isinstance(payload, dict):
            detail = str(payload.get("detail", "") or detail or "ok").strip()
            return bool(payload.get("ok")), detail
        return result.returncode == 0, detail or f"probe fallita rc={result.returncode}"

    def _probe_playwright_health(self, force: bool = False) -> dict:
        unavailable = {
            "checked_at": int(time.time()),
            "driver_ready": False,
            "engine_states": {engine: "missing" for engine in SUPPORTED_ENGINES},
            "browsers_dir": self._effective_browsers_dir(),
            "last_error": PLAYWRIGHT_INSTALL_MSG,
        }
        if not self._playwright_available():
            return unavailable

        browsers_dir = self._effective_browsers_dir()
        if not force:
            cached = self._load_probe_cache()
            if (
                cached
                and cached.get("browsers_dir") == browsers_dir
                and int(time.time()) - int(cached.get("checked_at", 0) or 0) <= PLAYWRIGHT_PROBE_TTL_SECONDS
            ):
                return cached

        driver_ready, driver_detail = self._run_playwright_probe("driver")
        engine_states: dict[str, str] = {}
        last_error = "" if driver_ready else f"Playwright driver non pronto: {driver_detail}"
        for engine in SUPPORTED_ENGINES:
            if not driver_ready:
                engine_states[engine] = "driver-unavailable"
                continue
            ok, detail = self._run_playwright_probe("engine", engine=engine)
            if ok:
                engine_states[engine] = "ready"
            else:
                engine_states[engine] = f"error: {detail}"
                if not last_error:
                    last_error = f"{engine} non pronto: {detail}"

        data = {
            "checked_at": int(time.time()),
            "driver_ready": driver_ready,
            "engine_states": engine_states,
            "browsers_dir": browsers_dir,
            "last_error": last_error,
        }
        self._store_probe_cache(data)
        return data

    def _ensure_playwright_runtime(self):
        if not self._playwright_available():
            raise RuntimeError(PLAYWRIGHT_INSTALL_MSG)
        probe = self._probe_playwright_health(force=False)
        if not probe.get("driver_ready"):
            raise RuntimeError(str(probe.get("last_error") or PLAYWRIGHT_INSTALL_MSG))
        self._ensure_playwright_vendor_path()
        effective_browsers = self._effective_browsers_dir()
        if effective_browsers:
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = effective_browsers
        if self._playwright_runtime is None:
            from playwright.sync_api import sync_playwright

            self._playwright_runtime = sync_playwright().start()
        return self._playwright_runtime

    def _status(self) -> BrowserStatus:
        info = ChromeDevToolsMCP._read_remote_debug_info()
        executable = ChromeDevToolsMCP._resolve_browser_executable()
        running = ChromeDevToolsMCP._is_browser_running(executable)
        playwright_available = self._playwright_available()
        probe = self._probe_playwright_health(force=False) if playwright_available else {
            "driver_ready": False,
            "engine_states": {engine: "missing" for engine in SUPPORTED_ENGINES},
            "last_error": PLAYWRIGHT_INSTALL_MSG,
        }
        default_engine = self._normalize_engine("")
        driver_ready = bool(probe.get("driver_ready"))
        engine_states = dict(probe.get("engine_states") or {engine: "unknown" for engine in SUPPORTED_ENGINES})
        return BrowserStatus(
            remote_debugging=bool(info),
            browser_running=running,
            browser_executable=executable,
            playwright_available=playwright_available,
            playwright_driver_ready=driver_ready,
            shared_ready=bool(info) and driver_ready,
            isolated_ready=driver_ready and engine_states.get(default_engine) == "ready",
            default_engine=default_engine,
            supported_engines=SUPPORTED_ENGINES,
            engine_states=engine_states,
            browsers_dir=self._effective_browsers_dir(),
            last_error=(
                str(probe.get("last_error") or "").strip()
                or self._last_error
                or getattr(self._devtools, "_last_error", "")
            ),
        )

    def status(self) -> str:
        return self._status().render()

    def _shared_pages(self) -> list:
        if self._shared_browser is None:
            return []
        pages = []
        for context in self._shared_browser.contexts:
            for page in context.pages:
                url = getattr(page, "url", "") or ""
                if str(url).startswith("devtools://"):
                    continue
                pages.append(page)
        return pages

    def _current_shared_page(self):
        pages = self._shared_pages()
        if not pages:
            context = self._shared_context
            if context is None:
                contexts = getattr(self._shared_browser, "contexts", [])
                context = contexts[0] if contexts else None
                self._shared_context = context
            if context is None:
                raise RuntimeError("Chrome condiviso connesso ma senza contesti disponibili.")
            page = context.new_page()
            pages = [page]
        if self._shared_page_index < 0 or self._shared_page_index >= len(pages):
            self._shared_page_index = len(pages) - 1
        page = pages[self._shared_page_index]
        try:
            page.bring_to_front()
        except Exception:
            pass
        return page

    def _ensure_shared_page(self, relaunch: bool = False, url: str = ""):
        if relaunch:
            self._shared_browser = None
            self._shared_context = None
            self._shared_page_index = -1
        ok, details = self._devtools._prepare_browser(relaunch=relaunch, url=url)
        if not ok:
            self._last_error = details
            raise RuntimeError(details)

        self._ensure_playwright_runtime()
        try:
            if self._shared_browser is not None:
                try:
                    _ = self._shared_browser.contexts
                except Exception:
                    self._shared_browser = None
                    self._shared_context = None
            if self._shared_browser is None:
                endpoint = f"http://127.0.0.1:{BROWSER_PORT}"
                self._shared_browser = self._playwright_runtime.chromium.connect_over_cdp(endpoint)
        except Exception as exc:
            self._shared_browser = None
            self._shared_context = None
            self._last_error = f"Connessione CDP fallita: {exc}"
            raise RuntimeError(self._last_error) from exc

        contexts = getattr(self._shared_browser, "contexts", [])
        self._shared_context = contexts[0] if contexts else None
        if url:
            page = self._current_shared_page()
            page.goto(url, wait_until="domcontentloaded")
            return page
        return self._current_shared_page()

    def _ensure_isolated_page(
        self,
        url: str = "",
        engine: str = "",
        channel: str = "",
        headless: Optional[bool] = None,
    ):
        runtime = self._ensure_playwright_runtime()
        resolved_engine = self._normalize_engine(engine)
        probe = self._probe_playwright_health(force=False)
        engine_state = str((probe.get("engine_states") or {}).get(resolved_engine, "") or "")
        if engine_state != "ready":
            raise RuntimeError(
                f"Engine Playwright `{resolved_engine}` non pronto: {engine_state or probe.get('last_error') or 'stato sconosciuto'}"
            )
        resolved_channel = self._normalize_channel(channel)
        resolved_headless = self._normalize_headless(headless)
        needs_restart = (
            self._isolated_browser is None
            or self._isolated_engine != resolved_engine
            or self._isolated_channel != resolved_channel
            or self._isolated_headless != resolved_headless
        )
        if needs_restart and self._isolated_browser is not None:
            self._isolated_browser.close()
            self._isolated_browser = None
            self._isolated_context = None
            self._isolated_page = None
        if self._isolated_browser is None:
            browser_type = getattr(runtime, resolved_engine)
            launch_kwargs = {"headless": resolved_headless}
            if resolved_engine == "chromium" and resolved_channel:
                launch_kwargs["channel"] = resolved_channel
            self._isolated_browser = browser_type.launch(**launch_kwargs)
            self._isolated_context = self._isolated_browser.new_context()
            self._isolated_context.set_default_timeout(15000)
            self._isolated_page = self._isolated_context.new_page()
            self._isolated_engine = resolved_engine
            self._isolated_channel = resolved_channel
            self._isolated_headless = resolved_headless
        page = self._isolated_page
        if url:
            page.goto(url, wait_until="domcontentloaded")
        return page

    def _resolve_mode(self, requested_mode: str, action: str, engine: str = "") -> str:
        status = self._status()
        return choose_browser_mode(
            requested_mode=self._normalize_mode(requested_mode),
            action=action,
            remote_debugging=status.remote_debugging,
            browser_running=status.browser_running,
            engine=self._normalize_engine(engine),
        )

    def connect(
        self,
        mode: str = "auto",
        url: str = "",
        engine: str = "",
        channel: str = "",
        headless: Optional[bool] = None,
    ) -> str:
        selected = self._resolve_mode(mode, "connect", engine=engine)
        if selected == "shared":
            page = self._ensure_shared_page(relaunch=False, url=url)
            return f"shared browser ready\nurl: {page.url}"
        page = self._ensure_isolated_page(url=url, engine=engine, channel=channel, headless=headless)
        return f"isolated browser ready ({self._isolated_engine}{('/' + self._isolated_channel) if self._isolated_channel else ''})\nurl: {page.url}"

    def relaunch(self, url: str = "") -> str:
        page = self._ensure_shared_page(relaunch=True, url=url)
        return f"shared browser relaunched\nurl: {page.url}"

    def list_pages(self, mode: str = "shared", engine: str = "") -> str:
        selected = self._resolve_mode(mode, "list_pages", engine=engine)
        if selected != "shared":
            page = self._ensure_isolated_page(engine=engine)
            return f"* [0] {self._safe_title(page)} | {page.url}"

        self._ensure_shared_page()
        lines = []
        for idx, page in enumerate(self._shared_pages()):
            marker = "*" if idx == self._shared_page_index else "-"
            lines.append(f"{marker} [{idx}] {self._safe_title(page)} | {page.url}")
        return "\n".join(lines) if lines else "(no pages)"

    def select_page(self, index: int, mode: str = "shared", engine: str = "") -> str:
        selected = self._resolve_mode(mode, "select_page", engine=engine)
        if selected != "shared":
            raise RuntimeError("select_page richiede mode=shared")
        self._ensure_shared_page()
        pages = self._shared_pages()
        if index < 0 or index >= len(pages):
            raise RuntimeError(f"Pagina non valida: {index}")
        self._shared_page_index = index
        page = self._current_shared_page()
        return f"selected page [{index}] {self._safe_title(page)} | {page.url}"

    def navigate(
        self,
        url: str,
        mode: str = "auto",
        engine: str = "",
        channel: str = "",
        headless: Optional[bool] = None,
    ) -> str:
        if not url:
            raise RuntimeError("URL obbligatorio")
        selected = self._resolve_mode(mode, "navigate", engine=engine)
        page = (
            self._ensure_shared_page(url=url)
            if selected == "shared"
            else self._ensure_isolated_page(url=url, engine=engine, channel=channel, headless=headless)
        )
        return self._render_page(page)

    def new_page(
        self,
        url: str = "",
        mode: str = "auto",
        engine: str = "",
        channel: str = "",
        headless: Optional[bool] = None,
    ) -> str:
        selected = self._resolve_mode(mode, "navigate", engine=engine)
        if selected == "shared":
            self._ensure_shared_page()
            context = self._shared_context
            if context is None:
                raise RuntimeError("Contesto shared non disponibile")
            page = context.new_page()
            self._shared_page_index = max(0, len(self._shared_pages()) - 1)
            if url:
                page.goto(url, wait_until="domcontentloaded")
            return self._render_page(page)
        page = self._ensure_isolated_page(engine=engine, channel=channel, headless=headless)
        page = self._isolated_context.new_page()
        self._isolated_page = page
        if url:
            page.goto(url, wait_until="domcontentloaded")
        return self._render_page(page)

    def read(self, mode: str = "auto", engine: str = "", channel: str = "", headless: Optional[bool] = None) -> str:
        selected = self._resolve_mode(mode, "read", engine=engine)
        page = (
            self._ensure_shared_page()
            if selected == "shared"
            else self._ensure_isolated_page(engine=engine, channel=channel, headless=headless)
        )
        return self._render_page(page)

    def click(
        self,
        selector: str,
        mode: str = "auto",
        engine: str = "",
        channel: str = "",
        headless: Optional[bool] = None,
    ) -> str:
        if not selector:
            raise RuntimeError("selector obbligatorio")
        selected = self._resolve_mode(mode, "click", engine=engine)
        page = (
            self._ensure_shared_page()
            if selected == "shared"
            else self._ensure_isolated_page(engine=engine, channel=channel, headless=headless)
        )
        page.locator(selector).first.click()
        page.wait_for_load_state("domcontentloaded")
        return f"clicked: {selector}\nurl: {page.url}"

    def fill(
        self,
        selector: str,
        text: str,
        mode: str = "auto",
        engine: str = "",
        channel: str = "",
        headless: Optional[bool] = None,
    ) -> str:
        if not selector:
            raise RuntimeError("selector obbligatorio")
        selected = self._resolve_mode(mode, "fill", engine=engine)
        page = (
            self._ensure_shared_page()
            if selected == "shared"
            else self._ensure_isolated_page(engine=engine, channel=channel, headless=headless)
        )
        page.locator(selector).first.fill(text or "")
        return f"filled: {selector}"

    def type_text(
        self,
        selector: str,
        text: str,
        mode: str = "auto",
        engine: str = "",
        channel: str = "",
        headless: Optional[bool] = None,
    ) -> str:
        if not selector:
            raise RuntimeError("selector obbligatorio")
        selected = self._resolve_mode(mode, "type", engine=engine)
        page = (
            self._ensure_shared_page()
            if selected == "shared"
            else self._ensure_isolated_page(engine=engine, channel=channel, headless=headless)
        )
        page.locator(selector).first.type(text or "")
        return f"typed into: {selector}"

    def wait(
        self,
        selector: str = "",
        timeout_ms: int = 4000,
        mode: str = "auto",
        engine: str = "",
        channel: str = "",
        headless: Optional[bool] = None,
    ) -> str:
        selected = self._resolve_mode(mode, "wait", engine=engine)
        page = (
            self._ensure_shared_page()
            if selected == "shared"
            else self._ensure_isolated_page(engine=engine, channel=channel, headless=headless)
        )
        if selector:
            page.locator(selector).first.wait_for(timeout=max(500, timeout_ms))
            return f"selector ready: {selector}"
        page.wait_for_timeout(max(250, timeout_ms))
        return f"waited {max(250, timeout_ms)}ms"

    def evaluate(
        self,
        js: str,
        mode: str = "auto",
        engine: str = "",
        channel: str = "",
        headless: Optional[bool] = None,
    ) -> str:
        if not js:
            raise RuntimeError("js obbligatorio")
        selected = self._resolve_mode(mode, "evaluate", engine=engine)
        page = (
            self._ensure_shared_page()
            if selected == "shared"
            else self._ensure_isolated_page(engine=engine, channel=channel, headless=headless)
        )
        result = page.evaluate(js)
        return str(result)

    def screenshot(
        self,
        path: str = "",
        mode: str = "auto",
        engine: str = "",
        channel: str = "",
        headless: Optional[bool] = None,
    ) -> str:
        selected = self._resolve_mode(mode, "screenshot", engine=engine)
        page = (
            self._ensure_shared_page()
            if selected == "shared"
            else self._ensure_isolated_page(engine=engine, channel=channel, headless=headless)
        )
        os.makedirs(CAPTURES_DIR, exist_ok=True)
        target = path or os.path.join(CAPTURES_DIR, f"browser_{int(time.time())}.png")
        page.screenshot(path=target, full_page=True)
        return target

    def close(self, mode: str = "auto", engine: str = "") -> str:
        selected = self._resolve_mode(mode, "close", engine=engine)
        if selected == "shared":
            self._shared_browser = None
            self._shared_context = None
            self._shared_page_index = -1
            return "shared browser detached"
        if self._isolated_browser is not None:
            self._isolated_browser.close()
        self._isolated_browser = None
        self._isolated_context = None
        self._isolated_page = None
        self._isolated_engine = ""
        self._isolated_channel = ""
        return "isolated browser closed"

    def _safe_title(self, page) -> str:
        try:
            return page.title()
        except Exception:
            return "(untitled)"

    def _render_page(self, page) -> str:
        title = self._safe_title(page)
        text = ""
        try:
            text = page.locator("body").inner_text(timeout=4000)
        except Exception:
            try:
                text = page.inner_text("body")
            except Exception:
                text = ""
        text = str(text or "").strip()
        if len(text) > 12000:
            text = text[:12000] + "\n[...troncato]"
        return f"[{title}]\n{page.url}\n\n{text}"


_browser_manager: Optional[BrowserManager] = None


def get_browser_manager() -> BrowserManager:
    global _browser_manager
    if _browser_manager is None:
        _browser_manager = BrowserManager()
    return _browser_manager
