"""
openvurp Core — MCP Client (Model Context Protocol)

Client minimale per connettersi a server MCP via stdio.
Scopre tool remoti e li rende disponibili come tool openvurp.
"""

from __future__ import annotations

import os
import json
import subprocess
import threading
import uuid
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class MCPServer:
    """Configurazione di un server MCP."""
    name: str
    command: str  # es: "npx -y @modelcontextprotocol/server-filesystem /"
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    tools: list[dict] = field(default_factory=list)
    process: Optional[subprocess.Popen] = None
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _request_id: int = 0


class MCPClient:
    """Client MCP che gestisce piu server e i loro tool."""

    def __init__(self):
        self.servers: dict[str, MCPServer] = {}

    def add_server(self, name: str, command: str, args: list[str] = None,
                   env: dict[str, str] = None) -> MCPServer:
        """Aggiunge e avvia un server MCP."""
        server = MCPServer(
            name=name,
            command=command,
            args=args or [],
            env=env or {},
        )
        self.servers[name] = server
        return server

    def connect(self, name: str) -> bool:
        """Avvia il processo MCP e fa handshake."""
        server = self.servers.get(name)
        if not server:
            return False

        try:
            cmd = [server.command] + server.args
            merged_env = {**os.environ, **server.env}

            server.process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=merged_env,
            )

            # Initialize handshake
            init_result = self._send_request(server, "initialize", {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "openvurp", "version": "4.0"}
            })

            if not init_result:
                return False

            # Send initialized notification
            self._send_notification(server, "notifications/initialized", {})

            # Discover tools
            tools_result = self._send_request(server, "tools/list", {})
            if tools_result and "tools" in tools_result:
                server.tools = tools_result["tools"]

            return True

        except Exception:
            return False

    def call_tool(self, server_name: str, tool_name: str, args: dict) -> str:
        """Chiama un tool su un server MCP."""
        server = self.servers.get(server_name)
        if not server or not server.process:
            return f"[Errore: server MCP '{server_name}' non connesso]"

        try:
            result = self._send_request(server, "tools/call", {
                "name": tool_name,
                "arguments": args,
            })

            if not result:
                return "[Errore: nessuna risposta dal server MCP]"

            # Estrai contenuto dalla risposta
            content_parts = result.get("content", [])
            texts = []
            for part in content_parts:
                if isinstance(part, dict) and part.get("type") == "text":
                    texts.append(part.get("text", ""))
            return "\n".join(texts) if texts else json.dumps(result)

        except Exception as e:
            return f"[Errore MCP: {e}]"

    def get_all_tools(self) -> list[dict]:
        """Lista tutti i tool da tutti i server connessi."""
        all_tools = []
        for server_name, server in self.servers.items():
            for tool in server.tools:
                tool_info = dict(tool)
                tool_info["_mcp_server"] = server_name
                all_tools.append(tool_info)
        return all_tools

    def disconnect(self, name: str):
        """Termina un server MCP."""
        server = self.servers.get(name)
        if server and server.process:
            try:
                server.process.terminate()
                server.process.wait(timeout=5)
            except Exception:
                try:
                    server.process.kill()
                except Exception:
                    pass
            server.process = None

    def disconnect_all(self):
        """Termina tutti i server."""
        for name in list(self.servers.keys()):
            self.disconnect(name)

    def _send_request(self, server: MCPServer, method: str, params: dict) -> dict | None:
        """Invia JSON-RPC request e attende risposta."""
        if not server.process or not server.process.stdin or not server.process.stdout:
            return None

        with server._lock:
            server._request_id += 1
            req_id = server._request_id

            request = {
                "jsonrpc": "2.0",
                "id": req_id,
                "method": method,
                "params": params,
            }

            try:
                line = json.dumps(request) + "\n"
                server.process.stdin.write(line.encode())
                server.process.stdin.flush()

                # Leggi risposta (con timeout semplice via readline)
                response_line = server.process.stdout.readline()
                if not response_line:
                    return None

                response = json.loads(response_line.decode().strip())
                return response.get("result", None)

            except Exception:
                return None

    def _send_notification(self, server: MCPServer, method: str, params: dict):
        """Invia notifica (no response attesa)."""
        if not server.process or not server.process.stdin:
            return

        notification = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
        }

        try:
            line = json.dumps(notification) + "\n"
            server.process.stdin.write(line.encode())
            server.process.stdin.flush()
        except Exception:
            pass

    @staticmethod
    def from_config(config_path: str) -> "MCPClient":
        """Carica configurazione MCP da file JSON.

        Formato:
        {
            "servers": {
                "filesystem": {
                    "command": "npx",
                    "args": ["-y", "@modelcontextprotocol/server-filesystem", "/home"],
                    "env": {}
                }
            }
        }
        """
        client = MCPClient()

        if not os.path.exists(config_path):
            return client

        try:
            with open(config_path, "r", encoding="utf-8") as f:
                config = json.load(f)

            for name, srv_config in config.get("servers", {}).items():
                client.add_server(
                    name=name,
                    command=srv_config.get("command", ""),
                    args=srv_config.get("args", []),
                    env=srv_config.get("env", {}),
                )
        except Exception:
            pass

        return client
