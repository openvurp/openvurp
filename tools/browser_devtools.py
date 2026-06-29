"""
openvurp Tools — Chrome DevTools MCP

Controllo browser via Chrome DevTools Protocol + MCP.
Meglio di Playwright perché:
- Riusa la sessione browser dell'utente (già loggato ovunque)
- Zero browser headless da gestire
- Ispeziona DevTools reali (network, elements, console)
- Protocollo MCP standard

Richiede: npx chrome-devtools-mcp@latest
Chrome >= 144 per autoConnect (attualmente beta)
"""

from __future__ import annotations

import glob
import json
import os
import platform
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from typing import Optional
from core.tools import Tool, ToolResult, RetryPolicy


OPENVURP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_DIR = os.path.join(OPENVURP_DIR, "memory", "cache")
BROWSER_CACHE_PATH = os.path.join(CACHE_DIR, "browser_devtools.json")
BROWSER_PORT = 9222
BROWSER_CANDIDATE_BASENAMES = (
    "chrome.exe",
    "chrome",
    "msedge.exe",
    "msedge",
    "brave.exe",
    "brave",
    "brave-browser",
    "vivaldi.exe",
    "vivaldi",
    "opera.exe",
    "opera",
    "launcher.exe",
    "chromium",
    "chromium-browser",
)


class ChromeDevToolsMCP:
    """
    Client per Chrome DevTools MCP server.
    Si connette al browser Chrome dell'utente via MCP.
    """

    def __init__(self, auto_connect: bool = True, channel: str = ""):
        self._process = None
        self._lock = threading.Lock()
        self._request_id = 0
        self._connected = False
        self._auto_connect = auto_connect
        self._channel = channel  # es. "beta" per Chrome beta
        self._tools: list[dict] = []
        self._last_error = ""
        self._last_browser_path = ""

    @staticmethod
    def _cache_path() -> str:
        os.makedirs(CACHE_DIR, exist_ok=True)
        return BROWSER_CACHE_PATH

    @staticmethod
    def _load_cached_browser_path() -> str:
        path = ChromeDevToolsMCP._cache_path()
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return ""
        candidate = str(data.get("path", "") or "").strip()
        return candidate if ChromeDevToolsMCP._looks_launchable(candidate) else ""

    @staticmethod
    def _store_cached_browser_path(path: str) -> None:
        if not path:
            return
        try:
            with open(ChromeDevToolsMCP._cache_path(), "w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "path": path,
                        "platform": platform.system(),
                        "updated_at": int(time.time()),
                    },
                    handle,
                    indent=2,
                )
        except OSError:
            pass

    @staticmethod
    def _looks_launchable(candidate: str) -> bool:
        if not candidate:
            return False
        expanded = os.path.expandvars(os.path.expanduser(candidate))
        if os.path.isabs(expanded) or os.sep in expanded or "/" in expanded or "\\" in expanded:
            return os.path.exists(expanded)
        return shutil.which(expanded) is not None

    @staticmethod
    def _windows_registry_browser_paths() -> list[str]:
        try:
            import winreg  # type: ignore
        except ImportError:
            return []

        subkeys = [
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe",
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe",
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\brave.exe",
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\brave-browser.exe",
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\vivaldi.exe",
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\launcher.exe",
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chromium.exe",
        ]
        roots = [winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE]
        paths: list[str] = []
        for root in roots:
            for subkey in subkeys:
                try:
                    with winreg.OpenKey(root, subkey) as key:
                        value, _kind = winreg.QueryValueEx(key, None)
                except OSError:
                    continue
                if isinstance(value, str) and value.strip():
                    paths.append(value.strip())
        return paths

    @staticmethod
    def _wsl_localappdata_candidates() -> list[str]:
        candidates: list[str] = []
        patterns = [
            "/mnt/c/Users/*/AppData/Local/Google/Chrome/Application/chrome.exe",
            "/mnt/c/Users/*/AppData/Local/Microsoft/Edge/Application/msedge.exe",
            "/mnt/c/Users/*/AppData/Local/BraveSoftware/Brave-Browser/Application/brave.exe",
            "/mnt/c/Users/*/AppData/Local/Vivaldi/Application/vivaldi.exe",
            "/mnt/c/Users/*/AppData/Local/Programs/Opera/launcher.exe",
            "/mnt/c/Users/*/AppData/Local/Chromium/Application/chrome.exe",
        ]
        for pattern in patterns:
            candidates.extend(glob.glob(pattern))
        return candidates

    @staticmethod
    def _candidate_chrome_paths(system: str = "", env: dict | None = None) -> list[str]:
        env_map = env or os.environ
        current_system = system or platform.system()
        candidates: list[str] = []

        for key in ("OPENVURP_BROWSER_PATH", "CHROME_PATH", "BROWSER"):
            override = str(env_map.get(key, "") or "").strip()
            if override:
                candidates.append(override)

        cached = ChromeDevToolsMCP._load_cached_browser_path()
        if cached:
            candidates.append(cached)

        if current_system == "Windows":
            for registry_path in ChromeDevToolsMCP._windows_registry_browser_paths():
                candidates.append(registry_path)
            for root in (
                env_map.get("ProgramFiles"),
                env_map.get("ProgramFiles(x86)"),
                env_map.get("LOCALAPPDATA"),
            ):
                if not root:
                    continue
                candidates.extend(
                    [
                        os.path.join(root, "Google", "Chrome", "Application", "chrome.exe"),
                        os.path.join(root, "Google", "Chrome Beta", "Application", "chrome.exe"),
                        os.path.join(root, "Microsoft", "Edge", "Application", "msedge.exe"),
                        os.path.join(root, "BraveSoftware", "Brave-Browser", "Application", "brave.exe"),
                        os.path.join(root, "Vivaldi", "Application", "vivaldi.exe"),
                        os.path.join(root, "Programs", "Opera", "launcher.exe"),
                        os.path.join(root, "Chromium", "Application", "chrome.exe"),
                    ]
                )
            for name in BROWSER_CANDIDATE_BASENAMES:
                found = shutil.which(name)
                if found:
                    candidates.append(found)
        elif os.path.exists("/mnt/c"):
            candidates.extend(
                [
                    "/mnt/c/Program Files/Google/Chrome/Application/chrome.exe",
                    "/mnt/c/Program Files/Google/Chrome Beta/Application/chrome.exe",
                    "/mnt/c/Program Files (x86)/Google/Chrome/Application/chrome.exe",
                    "/mnt/c/Program Files/Microsoft/Edge/Application/msedge.exe",
                    "/mnt/c/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
                    "/mnt/c/Program Files/BraveSoftware/Brave-Browser/Application/brave.exe",
                    "/mnt/c/Program Files (x86)/BraveSoftware/Brave-Browser/Application/brave.exe",
                    "/mnt/c/Program Files/Vivaldi/Application/vivaldi.exe",
                    "/mnt/c/Program Files (x86)/Vivaldi/Application/vivaldi.exe",
                ]
            )
            candidates.extend(ChromeDevToolsMCP._wsl_localappdata_candidates())
        elif current_system == "Darwin":
            candidates.extend(
                [
                    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                    "/Applications/Google Chrome Beta.app/Contents/MacOS/Google Chrome Beta",
                    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
                    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
                    "/Applications/Vivaldi.app/Contents/MacOS/Vivaldi",
                    "/Applications/Opera.app/Contents/MacOS/Opera",
                    "/Applications/Chromium.app/Contents/MacOS/Chromium",
                ]
            )
        else:
            for name in (
                "google-chrome-stable",
                "google-chrome",
                "chromium-browser",
                "chromium",
                "microsoft-edge",
                "brave-browser",
                "vivaldi",
                "opera",
            ):
                found = shutil.which(name)
                if found:
                    candidates.append(found)
                else:
                    candidates.append(name)

        seen: set[str] = set()
        unique: list[str] = []
        for candidate in candidates:
            expanded = os.path.expandvars(os.path.expanduser(str(candidate or "").strip()))
            if not expanded or expanded in seen:
                continue
            seen.add(expanded)
            unique.append(expanded)
        return unique

    @staticmethod
    def _resolve_browser_executable(system: str = "", env: dict | None = None) -> str:
        for candidate in ChromeDevToolsMCP._candidate_chrome_paths(system=system, env=env):
            if not ChromeDevToolsMCP._looks_launchable(candidate):
                continue
            if not (os.path.isabs(candidate) or os.sep in candidate or "/" in candidate or "\\" in candidate):
                resolved = shutil.which(candidate)
                if resolved:
                    ChromeDevToolsMCP._store_cached_browser_path(resolved)
                    return resolved
            ChromeDevToolsMCP._store_cached_browser_path(candidate)
            return candidate
        return ""

    @staticmethod
    def _browser_process_names(executable_path: str = "") -> list[str]:
        names: set[str] = set()
        if executable_path:
            base = os.path.basename(executable_path).lower()
            if base:
                names.add(base)
                if base.endswith(".exe"):
                    names.add(base.removesuffix(".exe"))
                else:
                    names.add(f"{base}.exe")
        else:
            names.update(name.lower() for name in BROWSER_CANDIDATE_BASENAMES)
        return sorted(name for name in names if name)

    @staticmethod
    def _read_remote_debug_info(port: int = BROWSER_PORT, timeout_seconds: float = 0.5) -> Optional[dict]:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/json/version",
                timeout=max(0.25, timeout_seconds),
            ) as response:
                payload = response.read().decode("utf-8", errors="replace")
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            return None
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            return None
        return data if isinstance(data, dict) else None

    @staticmethod
    def _wait_for_remote_debug(timeout_seconds: float = 8.0, port: int = BROWSER_PORT) -> bool:
        deadline = time.time() + max(1.0, timeout_seconds)
        while time.time() < deadline:
            if ChromeDevToolsMCP._read_remote_debug_info(port=port):
                return True
            time.sleep(0.25)
        return False

    @staticmethod
    def _is_browser_running(executable_path: str = "", system: str = "") -> bool:
        current_system = system or platform.system()
        names = ChromeDevToolsMCP._browser_process_names(executable_path)
        try:
            if current_system == "Windows":
                result = subprocess.run(
                    ["tasklist"],
                    capture_output=True,
                    text=True,
                    timeout=0.5,
                    check=False,
                )
                haystack = (result.stdout + "\n" + result.stderr).lower()
                return any(name in haystack for name in names)
            if os.path.exists("/mnt/c") and shutil.which("tasklist.exe"):
                result = subprocess.run(
                    ["tasklist.exe"],
                    capture_output=True,
                    text=True,
                    timeout=0.5,
                    check=False,
                )
                haystack = (result.stdout + "\n" + result.stderr).lower()
                return any(name in haystack for name in names)
            result = subprocess.run(
                ["ps", "-A", "-o", "comm="],
                capture_output=True,
                text=True,
                timeout=0.5,
                check=False,
            )
            processes = {line.strip().lower() for line in result.stdout.splitlines() if line.strip()}
            normalized = {name.removesuffix(".exe") for name in names}
            return any(proc in normalized or f"{proc}.exe" in names for proc in processes)
        except Exception:
            return False

    @staticmethod
    def _kill_browser_processes(executable_path: str = "", system: str = "") -> bool:
        current_system = system or platform.system()
        names = ChromeDevToolsMCP._browser_process_names(executable_path)
        basenames = sorted({name for name in names if name})
        killed_any = False

        for name in basenames:
            try:
                if current_system == "Windows":
                    result = subprocess.run(
                        ["taskkill", "/IM", name, "/F"],
                        capture_output=True,
                        text=True,
                        timeout=8,
                        check=False,
                    )
                    killed_any = killed_any or result.returncode == 0
                    continue
                if os.path.exists("/mnt/c") and shutil.which("taskkill.exe"):
                    result = subprocess.run(
                        ["taskkill.exe", "/IM", name, "/F"],
                        capture_output=True,
                        text=True,
                        timeout=8,
                        check=False,
                    )
                    killed_any = killed_any or result.returncode == 0
                    continue
                result = subprocess.run(
                    ["pkill", "-f", name.removesuffix(".exe")],
                    capture_output=True,
                    text=True,
                    timeout=8,
                    check=False,
                )
                killed_any = killed_any or result.returncode == 0
            except Exception:
                continue

        if killed_any:
            time.sleep(1.0)
        return killed_any

    @staticmethod
    def _launch_browser(executable_path: str, url: str = "") -> bool:
        if not executable_path:
            return False
        args = [executable_path, f"--remote-debugging-port={BROWSER_PORT}"]
        if url:
            args.append(url)
        kwargs = {
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        }
        if platform.system() == "Windows":
            kwargs["creationflags"] = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
                subprocess,
                "CREATE_NEW_PROCESS_GROUP",
                0,
            )
        try:
            subprocess.Popen(args, **kwargs)
            return True
        except (FileNotFoundError, OSError):
            return False

    @staticmethod
    def _build_mcp_command(
        npx_path: str = "",
        auto_connect: bool = True,
        channel: str = "",
        browser_url: str = "",
    ) -> list[str]:
        direct = shutil.which("chrome-devtools-mcp") or shutil.which("chrome-devtools-mcp.cmd")
        if direct:
            command = [direct]
        else:
            resolved_npx = npx_path or shutil.which("npx") or shutil.which("npx.cmd") or "npx"
            command = [resolved_npx, "-y", "chrome-devtools-mcp@latest"]
        if browser_url:
            command.append(f"--browser-url={browser_url}")
        elif auto_connect:
            command.append("--autoConnect")
        if channel:
            command.append(f"--channel={channel}")
        return command

    def _prepare_browser(self, relaunch: bool = False, url: str = "") -> tuple[bool, str]:
        info = self._read_remote_debug_info()
        if info and not relaunch:
            browser = str(info.get("Browser", "") or "Chromium")
            self._last_error = ""
            return True, f"Remote debugging already active on {browser}."

        executable_path = self._resolve_browser_executable()
        self._last_browser_path = executable_path
        if not executable_path:
            self._last_error = (
                "No Chromium browser found automatically. "
                "Imposta OPENVURP_BROWSER_PATH o CHROME_PATH se è installato in un percorso non standard."
            )
            return False, self._last_error

        if relaunch:
            self._kill_browser_processes(executable_path)
        elif self._is_browser_running(executable_path):
            self._last_error = (
                f"Browser already open without remote debugging: {executable_path}. "
                "Serve action=relaunch dopo approvazione utente, non una ricerca shell del path."
            )
            return False, self._last_error

        if not self._launch_browser(executable_path, url=url):
            self._last_error = f"Cannot start the browser: {executable_path}"
            return False, self._last_error

        if not self._wait_for_remote_debug():
            self._last_error = (
                f"Browser avviato ma endpoint DevTools non raggiungibile su 127.0.0.1:{BROWSER_PORT}."
            )
            return False, self._last_error

        self._last_error = ""
        return True, f"Browser pronto con remote debugging: {executable_path}"

    @property
    def is_connected(self) -> bool:
        return self._connected and self._process is not None

    def connect(self) -> bool:
        """Avvia il server MCP e connettiti a Chrome."""
        if self._connected:
            return True

        ok, details = self._prepare_browser(relaunch=False)
        if not ok and not (
            self._auto_connect and "Browser already open without remote debugging" in str(details)
        ):
            self._last_error = details
            return False

        try:
            browser_url = f"http://127.0.0.1:{BROWSER_PORT}" if self._read_remote_debug_info() else ""
            args = self._build_mcp_command(
                auto_connect=self._auto_connect and not browser_url,
                channel=self._channel,
                browser_url=browser_url,
            )

            self._process = subprocess.Popen(
                args,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            # MCP handshake
            init_result = self._send_request("initialize", {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "openvurp", "version": "4.0"},
            })

            if not init_result:
                self.disconnect()
                return False

            # Notifica initialized
            self._send_notification("notifications/initialized", {})

            # Scopri tool disponibili
            tools_result = self._send_request("tools/list", {})
            if tools_result and "tools" in tools_result:
                self._tools = tools_result["tools"]

            self._connected = True
            self._last_error = ""
            return True

        except FileNotFoundError:
            self._last_error = (
                "npx non trovato. Installa Node.js oppure aggiungi npx al PATH prima di usare browser_devtools."
            )
            return False
        except Exception:
            self._last_error = details or "Error while starting Chrome DevTools MCP."
            self.disconnect()
            return False

    def relaunch(self, url: str = "") -> tuple[bool, str]:
        self.disconnect()
        ok, details = self._prepare_browser(relaunch=True, url=url)
        if not ok:
            return False, details
        if self.connect():
            return True, details
        return False, self._last_error or details

    def status(self) -> str:
        info = self._read_remote_debug_info()
        executable = self._resolve_browser_executable()
        running = self._is_browser_running(executable)
        lines = [
            f"remote_debugging: {'on' if info else 'off'}",
            f"browser_running: {'yes' if running else 'no'}",
            f"browser_executable: {executable or '(not found)'}",
            f"mcp_connected: {'yes' if self.is_connected else 'no'}",
        ]
        if info:
            lines.append(f"browser: {info.get('Browser', '?')}")
            lines.append(f"websocket: {info.get('webSocketDebuggerUrl', '?')}")
        if self._last_error:
            lines.append(f"last_error: {self._last_error}")
        return "\n".join(lines)

    def disconnect(self):
        """Ferma il server MCP."""
        if self._process:
            try:
                self._process.terminate()
                self._process.wait(timeout=5)
            except Exception:
                try:
                    self._process.kill()
                except Exception:
                    pass
            self._process = None
        self._connected = False

    def call(self, tool_name: str, args: dict) -> str:
        """Chiama un tool Chrome DevTools via MCP."""
        if not self.is_connected:
            if not self.connect():
                return (
                    "[Chrome DevTools MCP non disponibile]\n"
                    f"{self._last_error or 'Cannot connect.'}\n"
                    "If the browser is already open without remote debugging, use action=relaunch after approval.\n"
                    "Se manca MCP: npx -y chrome-devtools-mcp@latest"
                )

        try:
            result = self._send_request("tools/call", {
                "name": tool_name,
                "arguments": args,
            })

            if not result:
                return "[Nessuna risposta da Chrome DevTools MCP]"

            # Estrai testo dalla risposta
            content_parts = result.get("content", [])
            texts = []
            for part in content_parts:
                if isinstance(part, dict) and part.get("type") == "text":
                    texts.append(part.get("text", ""))
            return "\n".join(texts) if texts else json.dumps(result, indent=2)

        except Exception as e:
            return f"[Chrome DevTools error: {e}]"

    def list_tools(self) -> list[dict]:
        """Lista tool disponibili dal server MCP."""
        if not self.is_connected:
            self.connect()
        return self._tools

    # ── MCP JSON-RPC ──

    def _send_request(self, method: str, params: dict) -> Optional[dict]:
        if not self._process or not self._process.stdin or not self._process.stdout:
            return None

        with self._lock:
            self._request_id += 1
            request = {
                "jsonrpc": "2.0",
                "id": self._request_id,
                "method": method,
                "params": params,
            }

            try:
                line = json.dumps(request) + "\n"
                self._process.stdin.write(line.encode())
                self._process.stdin.flush()

                response_line = self._process.stdout.readline()
                if not response_line:
                    return None

                response = json.loads(response_line.decode().strip())
                return response.get("result")
            except Exception:
                return None

    def _send_notification(self, method: str, params: dict):
        if not self._process or not self._process.stdin:
            return

        notification = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
        }

        try:
            line = json.dumps(notification) + "\n"
            self._process.stdin.write(line.encode())
            self._process.stdin.flush()
        except Exception:
            pass


# ── Singleton ──
_devtools: Optional[ChromeDevToolsMCP] = None


def _get_devtools() -> ChromeDevToolsMCP:
    global _devtools
    if _devtools is None:
        _devtools = ChromeDevToolsMCP()
    return _devtools


# ── Tool Handler ──

def browser_devtools_handler(action: str, url: str = None,
                              selector: str = None, js: str = None,
                              tool_name: str = None,
                              args: dict = None) -> ToolResult:
    """
    Handler per Chrome DevTools MCP.

    Azioni integrate:
    - status: Diagnosi rapida di browser, port 9222 e MCP
    - navigate: Naviga a un URL
    - inspect: Ispeziona elemento selezionato in DevTools
    - network: Mostra richieste di rete fallite
    - console: Mostra errori console
    - evaluate: Esegui JavaScript nella pagina
    - screenshot: Cattura screenshot
    - relaunch: Riavvia il browser con remote debugging
    - mcp_call: Chiama direttamente un tool MCP per nome
    - list_tools: Lista tool disponibili
    - connect: Connetti a Chrome
    - disconnect: Disconnetti
    """
    devtools = _get_devtools()

    try:
        if action == "connect":
            ok = devtools.connect()
            if ok:
                tools = devtools.list_tools()
                tool_names = [t.get("name", "?") for t in tools]
                return ToolResult.ok(
                    f"Connesso a Chrome DevTools.\n"
                    f"Tool disponibili: {', '.join(tool_names)}"
                )
            return ToolResult.fail(
                "Cannot connect to Chrome DevTools MCP.\n"
                f"{devtools._last_error or 'Verifica MCP e remote debugging.'}"
            )

        elif action == "status":
            return ToolResult.ok(devtools.status())

        elif action == "relaunch":
            ok, details = devtools.relaunch(url=url or "")
            if ok:
                return ToolResult.ok(details)
            return ToolResult.fail(details)

        elif action == "disconnect":
            devtools.disconnect()
            global _devtools
            _devtools = None
            return ToolResult.ok("Chrome DevTools disconnesso.")

        elif action == "list_tools":
            tools = devtools.list_tools()
            if not tools:
                return ToolResult.ok(
                    "No tools available. Connect first with action=connect."
                )
            lines = []
            for t in tools:
                name = t.get("name", "?")
                desc = t.get("description", "")[:100]
                lines.append(f"- {name}: {desc}")
            return ToolResult.ok("\n".join(lines))

        elif action == "navigate":
            if not url:
                return ToolResult.fail("URL obbligatorio per navigate")
            return ToolResult.ok(devtools.call("navigate_page", {"url": url}))

        elif action == "inspect":
            return ToolResult.ok(devtools.call("take_snapshot", {}))

        elif action == "network":
            return ToolResult.ok(devtools.call("list_network_requests", {}))

        elif action == "console":
            return ToolResult.ok(devtools.call("list_console_messages", {}))

        elif action == "evaluate":
            if not js:
                return ToolResult.fail("js obbligatorio per evaluate")
            return ToolResult.ok(devtools.call("evaluate_script", {"function": js}))

        elif action == "screenshot":
            return ToolResult.ok(devtools.call("take_screenshot", {}))

        elif action == "mcp_call":
            # Chiama qualsiasi tool MCP per nome
            if not tool_name:
                return ToolResult.fail("tool_name obbligatorio per mcp_call")
            return ToolResult.ok(devtools.call(tool_name, args or {}))

        else:
            return ToolResult.fail(
                f"Azione sconosciuta: {action}. "
                "Azioni: status, connect, relaunch, disconnect, list_tools, "
                "navigate, inspect, network, console, evaluate, screenshot, mcp_call"
            )

    except Exception as e:
        return ToolResult.fail(f"Chrome DevTools error: {e}")


BROWSER_DEVTOOLS_TOOL = Tool(
    name="browser_devtools",
    description=(
        "DEBUG e AUTOMAZIONE di Chrome via DevTools MCP: ispeziona console, "
        "network, DOM, esegue JavaScript, cattura screenshot, riusa la sessione "
        "reale dell'utente (gia' loggato). "
        "NON usarlo per cercare informazioni sul web — per quello usa `web_search`. "
        "NON usarlo per leggere una pagina statica — per quello usa `web_fetch`. "
        "Usalo SOLO se serve il DOM live, gli eventi DevTools, o interagire con "
        "una pagina dinamica gia' aperta dall'utente. "
        "Azioni: status, connect, relaunch, navigate, inspect, network, console, "
        "evaluate, screenshot, mcp_call, list_tools, disconnect."
    ),
    parameters={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "description": (
                    "Azione: status, connect, relaunch, navigate, inspect, network, "
                    "console, evaluate, screenshot, mcp_call, list_tools, disconnect"
                ),
            },
            "url": {"type": "string", "description": "URL per navigate o relaunch"},
            "selector": {"type": "string", "description": "CSS selector"},
            "js": {"type": "string", "description": "JavaScript per evaluate"},
            "tool_name": {"type": "string", "description": "Nome tool MCP per mcp_call"},
            "args": {"type": "object", "description": "Argomenti per mcp_call"},
        },
        "required": ["action"],
    },
    requires_approval=True,
    handler=browser_devtools_handler,
    timeout=60,
    retry_policy=RetryPolicy(max_retries=1),
)
