"""
openvurp Security — Sandbox

Esecuzione comandi in ambiente isolato.
Supporta: Docker container, nsjail, o fallback con restrizioni OS.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import tempfile
import json
from dataclasses import dataclass, field
from typing import Optional
from enum import Enum

from core.runtime_shell import (
    build_shell_command,
    default_allowed_env_vars,
    is_windows_platform,
    resolve_effective_shell,
    split_command_tokens,
)


class SandboxMode(Enum):
    """Modalità di sandboxing."""
    NONE = "none"           # Nessun sandbox (compatibilità)
    RESTRICTED = "restricted"  # Restrizioni OS (no shell=True, path jail)
    DOCKER = "docker"       # Container Docker isolato
    NSJAIL = "nsjail"       # nsjail (Linux only, più leggero di Docker)


@dataclass
class SandboxConfig:
    """Configurazione sandbox."""
    mode: SandboxMode = SandboxMode.RESTRICTED
    shell_executable: str = ""
    # Path jail - solo questi path sono accessibili
    allowed_paths: list[str] = field(default_factory=list)
    # Docker settings
    docker_image: str = "python:3.12-slim"
    docker_memory: str = "512m"
    docker_cpus: str = "1"
    docker_network: str = "none"  # none = nessun accesso rete
    docker_timeout: int = 120
    # Env vars da passare al sandbox (filtrate)
    allowed_env_vars: list[str] = field(default_factory=default_allowed_env_vars)
    # Comandi bloccati anche dentro il sandbox
    blocked_binaries: list[str] = field(default_factory=lambda: [
        "nc", "ncat", "netcat", "socat",  # reverse shell
        "nmap", "masscan",                  # scanning
        "base64",                           # encoding evasion (solo standalone)
    ])

    @classmethod
    def from_dict(cls, d: dict) -> "SandboxConfig":
        mode = SandboxMode(d.get("mode", "restricted"))
        cfg = cls(mode=mode)
        if "shell_executable" in d:
            cfg.shell_executable = d["shell_executable"]
        if "allowed_paths" in d:
            cfg.allowed_paths = d["allowed_paths"]
        if "docker_image" in d:
            cfg.docker_image = d["docker_image"]
        if "docker_memory" in d:
            cfg.docker_memory = d["docker_memory"]
        if "docker_cpus" in d:
            cfg.docker_cpus = d["docker_cpus"]
        if "docker_network" in d:
            cfg.docker_network = d["docker_network"]
        if "docker_timeout" in d:
            cfg.docker_timeout = d["docker_timeout"]
        if "allowed_env_vars" in d:
            cfg.allowed_env_vars = list(d["allowed_env_vars"] or [])
        return cfg


class Sandbox:
    """Sandbox per esecuzione sicura di comandi."""

    def __init__(self, config: SandboxConfig = None, working_dir: str = ""):
        self.config = config or SandboxConfig()
        self.working_dir = working_dir or os.getcwd()
        # Se nessun allowed_path, usa working_dir
        if not self.config.allowed_paths:
            self.config.allowed_paths = [self.working_dir]

    def execute(self, command: str, timeout: int = None,
                env: dict = None) -> tuple[str, int]:
        """
        Esegue un comando nel sandbox.
        Returns: (output, return_code)
        """
        timeout = timeout or self.config.docker_timeout

        if self.config.mode == SandboxMode.DOCKER:
            return self._execute_docker(command, timeout, env)
        elif self.config.mode == SandboxMode.NSJAIL:
            return self._execute_nsjail(command, timeout, env)
        elif self.config.mode == SandboxMode.RESTRICTED:
            return self._execute_restricted(command, timeout, env)
        else:
            # NONE mode - esecuzione diretta (legacy)
            return self._execute_direct(command, timeout, env)

    def check_path(self, path: str) -> tuple[bool, str]:
        """
        Verifica che un path sia dentro i path consentiti.
        Controlla anche symlink attacks.
        """
        try:
            # Risolvi il path reale (segui symlink)
            real_path = os.path.realpath(os.path.abspath(path))

            # Verifica che sia dentro un path consentito
            for allowed in self.config.allowed_paths:
                allowed_real = os.path.realpath(os.path.abspath(allowed))
                if real_path.startswith(allowed_real + os.sep) or real_path == allowed_real:
                    return True, ""

            return False, (
                f"Path fuori dal sandbox: {path} "
                f"(risolto a: {real_path}). "
                f"Path consentiti: {self.config.allowed_paths}"
            )
        except Exception as e:
            return False, f"Errore verifica path: {e}"

    def filter_env(self, env: dict = None) -> dict:
        """Filtra variabili d'ambiente, rimuove secrets."""
        base = {}
        for key in self.config.allowed_env_vars:
            val = os.environ.get(key)
            if val:
                base[key] = val
        # Aggiungi env custom ma solo se non contengono pattern sospetti
        if env:
            for key, val in env.items():
                key_lower = key.lower()
                # Blocca variabili che contengono secrets
                if any(s in key_lower for s in (
                    "key", "secret", "token", "password", "passwd",
                    "credential", "auth", "api_key", "apikey",
                )):
                    continue
                base[key] = val
        return base

    def _check_blocked_binaries(self, command: str) -> tuple[bool, str]:
        """Controlla se il comando usa binari bloccati."""
        resolved_shell = resolve_effective_shell(self.config.shell_executable)
        try:
            parts = split_command_tokens(
                command,
                shell_family=resolved_shell.family,
                platform_name=resolved_shell.platform,
            )
        except ValueError:
            parts = command.split()

        if not parts:
            return True, ""

        binary = os.path.basename(parts[0])
        if binary in self.config.blocked_binaries:
            return False, f"Binario bloccato nel sandbox: {binary}"

        # Controlla anche nei pipe
        for part in parts:
            if part == "|":
                continue
            base = os.path.basename(part)
            if base in self.config.blocked_binaries:
                return False, f"Binario bloccato nel sandbox: {base}"

        return True, ""

    def _execute_restricted(self, command: str, timeout: int,
                            env: dict = None) -> tuple[str, int]:
        """Esecuzione con restrizioni OS (no shell=True, env filtrato)."""
        # Check binari bloccati
        ok, reason = self._check_blocked_binaries(command)
        if not ok:
            return reason, 1

        safe_env = self.filter_env(env)
        resolved_shell = resolve_effective_shell(
            self.config.shell_executable,
            env=safe_env,
        )
        if resolved_shell.family == "posix":
            safe_env["HOME"] = self.working_dir
            safe_env["PWD"] = self.working_dir

        try:
            # Su Windows/cmd/PowerShell si passa sempre dalla shell esplicita
            # per evitare di rompere built-in e quoting.
            needs_shell = (
                resolved_shell.family in {"cmd", "powershell"}
                or any(c in command for c in ('|', '>', '<', '&&', '||', ';', '`', '$'))
            )
            if not needs_shell and resolved_shell.family == "posix":
                parts = split_command_tokens(
                    command,
                    shell_family=resolved_shell.family,
                    platform_name=resolved_shell.platform,
                )
                head = (parts[0] if parts else "").strip()
                if head in {"cd", "export", "alias", "source", "set", "unset"}:
                    needs_shell = True
                elif head and "=" in head and not head.startswith(("/", "./")):
                    key, _, value = head.partition("=")
                    if key and key.replace("_", "").isalnum() and value != "":
                        needs_shell = True

            if needs_shell:
                result = subprocess.run(
                    build_shell_command(command, resolved_shell),
                    capture_output=True,
                    timeout=timeout,
                    cwd=self.working_dir,
                    env=safe_env,
                    text=True,
                )
            else:
                args = split_command_tokens(
                    command,
                    shell_family=resolved_shell.family,
                    platform_name=resolved_shell.platform,
                )
                result = subprocess.run(
                    args,
                    capture_output=True,
                    timeout=timeout,
                    cwd=self.working_dir,
                    env=safe_env,
                    text=True,
                )

            output = result.stdout
            if result.stderr:
                output += ("\n" if output else "") + result.stderr
            return output.strip(), result.returncode

        except subprocess.TimeoutExpired:
            return f"[Timeout: comando interrotto dopo {timeout}s]", 124
        except FileNotFoundError as e:
            return f"[Comando non trovato: {e}]", 127
        except Exception as e:
            return f"[Errore sandbox: {e}]", 1

    def _execute_docker(self, command: str, timeout: int,
                        env: dict = None) -> tuple[str, int]:
        """Esecuzione in container Docker isolato."""
        if not self._docker_available():
            return "[Docker non disponibile. Installa Docker o usa mode=restricted]", 1

        safe_env = self.filter_env(env)

        # Costruisci comando docker run
        docker_args = [
            "docker", "run",
            "--rm",                                    # rimuovi dopo uso
            f"--memory={self.config.docker_memory}",   # limite memoria
            f"--cpus={self.config.docker_cpus}",       # limite CPU
            f"--network={self.config.docker_network}", # isolamento rete
            "--read-only",                             # filesystem read-only
            "--tmpfs", "/tmp:size=100m",               # /tmp scrivibile limitato
            "--no-new-privileges",                     # no privilege escalation
            "--security-opt=no-new-privileges:true",
            "-w", "/workspace",                        # working directory
        ]

        # Mount working directory
        docker_args.extend(["-v", f"{self.working_dir}:/workspace:rw"])

        # Env vars filtrate
        for key, val in safe_env.items():
            docker_args.extend(["-e", f"{key}={val}"])

        # Immagine e comando
        docker_args.extend([self.config.docker_image, "sh", "-c", command])

        try:
            result = subprocess.run(
                docker_args,
                capture_output=True,
                timeout=timeout + 10,  # extra per startup container
                text=True,
            )

            output = result.stdout
            if result.stderr:
                # Filtra warning Docker
                stderr_lines = [
                    l for l in result.stderr.splitlines()
                    if not l.startswith("WARNING:") and not l.startswith("Unable to find image")
                ]
                if stderr_lines:
                    output += ("\n" if output else "") + "\n".join(stderr_lines)

            return output.strip(), result.returncode

        except subprocess.TimeoutExpired:
            return f"[Timeout Docker: {timeout}s]", 124
        except Exception as e:
            return f"[Errore Docker: {e}]", 1

    def _execute_nsjail(self, command: str, timeout: int,
                        env: dict = None) -> tuple[str, int]:
        """Esecuzione con nsjail (Linux only)."""
        if not self._nsjail_available():
            return "[nsjail non disponibile. Installa nsjail o usa mode=restricted]", 1

        safe_env = self.filter_env(env)

        nsjail_args = [
            "nsjail",
            "--mode", "o",                     # once mode
            "--time_limit", str(timeout),
            "--rlimit_as", "512",              # 512MB address space
            "--rlimit_cpu", str(timeout),
            "--rlimit_fsize", "100",           # 100MB max file size
            "--rlimit_nofile", "256",          # max open files
            "--disable_clone_newnet",          # no network
            "--cwd", "/workspace",
        ]

        # Bind mount
        nsjail_args.extend(["-R", "/usr", "-R", "/lib", "-R", "/lib64",
                            "-R", "/bin", "-R", "/sbin",
                            "-B", f"{self.working_dir}:/workspace"])

        # Env
        for key, val in safe_env.items():
            nsjail_args.extend(["-E", f"{key}={val}"])

        nsjail_args.extend(["--", "sh", "-c", command])

        try:
            result = subprocess.run(
                nsjail_args,
                capture_output=True,
                timeout=timeout + 5,
                text=True,
            )
            output = result.stdout
            if result.stderr:
                output += ("\n" if output else "") + result.stderr
            return output.strip(), result.returncode
        except subprocess.TimeoutExpired:
            return f"[Timeout nsjail: {timeout}s]", 124
        except Exception as e:
            return f"[Errore nsjail: {e}]", 1

    def _execute_direct(self, command: str, timeout: int,
                        env: dict = None) -> tuple[str, int]:
        """Esecuzione diretta senza sandbox (legacy/compatibilità)."""
        safe_env = self.filter_env(env)
        resolved_shell = resolve_effective_shell(
            self.config.shell_executable,
            env=safe_env,
        )
        if resolved_shell.family == "posix":
            safe_env["HOME"] = self.working_dir
            safe_env["PWD"] = self.working_dir
        try:
            result = subprocess.run(
                build_shell_command(command, resolved_shell),
                capture_output=True,
                timeout=timeout,
                cwd=self.working_dir,
                text=True,
                env=safe_env,
            )
            output = result.stdout
            if result.stderr:
                output += ("\n" if output else "") + result.stderr
            return output.strip(), result.returncode
        except subprocess.TimeoutExpired:
            return f"[Timeout: {timeout}s]", 124
        except Exception as e:
            return f"[Errore: {e}]", 1

    @staticmethod
    def _docker_available() -> bool:
        try:
            r = subprocess.run(["docker", "info"], capture_output=True, timeout=5)
            return r.returncode == 0
        except Exception:
            return False

    @staticmethod
    def _nsjail_available() -> bool:
        try:
            r = subprocess.run(["nsjail", "--help"], capture_output=True, timeout=5)
            return r.returncode == 0
        except Exception:
            return False
