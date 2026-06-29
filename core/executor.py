"""
openvurp Core — Executor

Esecuzione tool con safety checks, retry, hooks, error classification.
"""

from __future__ import annotations

import time
import json
from typing import Any, Optional

from core.tools import Tool, ToolResult, ToolRegistry, ErrorType
from core.security.policy import ToolPolicyEngine
from core.security.audit import AuditAction


class Executor:
    def __init__(self, registry: ToolRegistry, safety=None, observer=None,
                 audit_log=None, lease_manager=None):
        self.registry = registry
        self.safety = safety
        self.observer = observer
        self.audit_log = audit_log
        self.lease_manager = lease_manager
        self.policy = ToolPolicyEngine(safety)

    def execute(self, tool_name: str, args: dict, ui=None,
                preapproved: bool = False, actor: str = "agent",
                source: str = "cli") -> ToolResult:
        """Esegue un tool con safety check, timing e logging."""
        tool = self.registry.get(tool_name)
        if not tool:
            result = ToolResult.fail(
                f"Tool sconosciuto: {tool_name}",
                error_type=ErrorType.NOT_FOUND,
                tool_name=tool_name
            )
            self._audit_tool_result(tool_name, args, result, actor, source)
            return result

        if not tool.handler:
            result = ToolResult.fail(
                f"Tool {tool_name} non ha handler",
                error_type=ErrorType.RUNTIME,
                tool_name=tool_name
            )
            self._audit_tool_result(tool_name, args, result, actor, source)
            return result

        # Policy check
        decision = self.policy.evaluate(tool, args)
        if decision.blocked:
            result = ToolResult.fail(
                f"Bloccato: {decision.reason}",
                error_type=ErrorType.PERMISSION,
                tool_name=tool_name,
            )
            self._audit_policy_decision(
                tool_name, args, decision, AuditAction.PERMISSION_DENIED,
                actor, source, success=False, note="blocked",
            )
            return result

        # Validate parameters before asking for approval. A malformed request
        # should fail cheaply and should not create a permission prompt.
        validation_error = self._validate_params(tool, args)
        if validation_error:
            result = ToolResult.fail(
                validation_error,
                error_type=ErrorType.VALIDATION,
                tool_name=tool_name
            )
            self._audit_tool_result(tool_name, args, result, actor, source, decision)
            return result

        if self._is_dry_run(args):
            result = self._dry_run_result(tool, args, decision)
            if self.observer:
                self.observer.log_tool_call(tool_name, args, result)
            self._audit_tool_result(tool_name, args, result, actor, source, decision)
            return result

        if decision.needs_approval and not preapproved:
            lease = self._consume_capability_lease(
                tool_name, args, decision, actor=actor, source=source
            )
            if lease:
                self._audit_policy_decision(
                    tool_name, args, decision, AuditAction.PERMISSION_GRANTED,
                    actor, source, success=True, note=f"capability_lease:{lease.id}",
                )
            elif not ui:
                result = ToolResult.fail(
                    f"Richiede approvazione: {decision.reason}",
                    error_type=ErrorType.PERMISSION,
                    tool_name=tool_name,
                )
                self._audit_policy_decision(
                    tool_name, args, decision, AuditAction.PERMISSION_DENIED,
                    actor, source, success=False, note="missing_ui",
                )
                return result
            else:
                desc = self._describe_action(tool_name, args)
                prompt = "Azione che richiede approvazione"
                if decision.risk.value != "safe":
                    prompt += f" ({decision.risk.value})"
                if decision.reason:
                    prompt += f":\n  {decision.reason}"
                prompt += f"\n  {desc}"

                # "sempre" = sì + lease di 8h per questo tool: il sistema
                # ricorda l'approvazione invece di richiederla a ogni chiamata
                confirm_choice = getattr(ui, "confirm_choice", None)
                if confirm_choice:
                    choice = confirm_choice(prompt)
                else:
                    choice = "yes" if ui.confirm(prompt) else "no"

                if choice == "no":
                    result = ToolResult.fail(
                        "L'OWNER HA RIFIUTATO questa azione. Non è un errore "
                        "tecnico né un fallimento di esecuzione: è una scelta "
                        "deliberata dell'utente. Non ritentare la stessa azione "
                        "— chiedi il motivo o proponi un'alternativa.",
                        error_type=ErrorType.PERMISSION,
                        tool_name=tool_name,
                    )
                    self._audit_policy_decision(
                        tool_name, args, decision, AuditAction.PERMISSION_DENIED,
                        actor, source, success=False, note="user_denied",
                    )
                    return result

                if choice == "always" and self.lease_manager:
                    try:
                        command_prefix = ""
                        if tool_name in ("shell", "process_start"):
                            command = str(args.get("command", "") or "")
                            parts = command.split()
                            command_prefix = parts[0] if parts else ""
                        risk = decision.risk.value
                        if risk in ("critical", "safe"):
                            risk = "high"
                        lease = self.lease_manager.grant(
                            actor=actor,
                            source=source,
                            tool_name=tool_name,
                            risk=risk,
                            ttl_seconds=8 * 3600,
                            max_uses=50,
                            reason="approvazione 'sempre' dell'owner",
                            command_prefix=command_prefix,
                        )
                        if hasattr(ui, "status"):
                            label = tool_name + (f" ({command_prefix}…)" if command_prefix else "")
                            ui.status(
                                f"[approvazione ricordata per {label}: 8h o 50 usi — "
                                f"revoca con capability_lease revoke {lease.id}]"
                            )
                    except Exception:
                        pass

                self._audit_policy_decision(
                    tool_name, args, decision, AuditAction.PERMISSION_GRANTED,
                    actor, source, success=True,
                    note="user_approved_always" if choice == "always" else "user_approved",
                )
        elif decision.needs_approval and preapproved:
            self._audit_policy_decision(
                tool_name, args, decision, AuditAction.PERMISSION_GRANTED,
                actor, source, success=True, note="preapproved",
            )

        # Execute with retry
        result = self._execute_with_retry(tool, self._handler_args(tool, args))

        # Log
        if self.observer:
            self.observer.log_tool_call(tool_name, args, result)
        self._audit_tool_result(tool_name, args, result, actor, source, decision)

        return result

    def _execute_with_retry(self, tool: Tool, args: dict) -> ToolResult:
        """Esegue con retry policy."""
        max_attempts = 1 + tool.retry_policy.max_retries
        last_result = None

        for attempt in range(max_attempts):
            if attempt > 0:
                time.sleep(tool.retry_policy.backoff_seconds * attempt)

            start = time.time()
            try:
                result = tool.handler(**args)
                if not isinstance(result, ToolResult):
                    result = ToolResult.ok(str(result))
                result.tool_name = tool.name
                result.duration_ms = int((time.time() - start) * 1000)
                last_result = result

                if result.success:
                    return result

                # Check if retryable
                if not result.retryable or result.error_type not in tool.retry_policy.retryable_errors:
                    return result

            except Exception as e:
                duration = int((time.time() - start) * 1000)
                error_type = self._classify_exception(e)
                last_result = ToolResult.fail(
                    str(e),
                    error_type=error_type,
                    duration_ms=duration,
                    retryable=error_type in tool.retry_policy.retryable_errors,
                    tool_name=tool.name
                )
                if error_type not in tool.retry_policy.retryable_errors:
                    return last_result

        return last_result

    def _consume_capability_lease(self, tool_name: str, args: dict, decision,
                                  actor: str, source: str):
        if not self.lease_manager:
            return None
        try:
            lease = self.lease_manager.find_valid(
                actor=actor,
                source=source,
                tool_name=tool_name,
                args=args,
                risk=decision.risk.value,
            )
            if not lease:
                return None
            return self.lease_manager.consume(lease.id)
        except Exception:
            return None

    def _handler_args(self, tool: Tool, args: dict) -> dict:
        """Strip executor-only args unless the tool explicitly declares them."""
        if not isinstance(args, dict):
            return args
        props = {}
        if isinstance(tool.parameters, dict):
            props = tool.parameters.get("properties", {}) or {}
        if "dry_run" in args and "dry_run" not in props:
            cleaned = dict(args)
            cleaned.pop("dry_run", None)
            return cleaned
        return args

    def _dry_run_result(self, tool: Tool, args: dict, decision) -> ToolResult:
        action = self._dry_run_action(tool.name, args)
        lines = [
            "DRY RUN",
            f"tool: {tool.name}",
            f"risk: {decision.risk.value}",
        ]
        if decision.reason:
            lines.append(f"policy_note: {decision.reason}")
        lines.extend([
            f"would: {action}",
            "effect: no command was executed and no file, process, or external state was changed",
        ])
        return ToolResult.ok("\n".join(lines), tool_name=tool.name)

    def _dry_run_action(self, tool_name: str, args: dict) -> str:
        if tool_name in ("shell", "process_start"):
            command = self._preview_value(args.get("command", ""))
            workdir = args.get("workdir") or args.get("cwd") or "."
            return f"run command `{command}` in `{workdir}`"
        if tool_name == "process_write":
            text = str(args.get("text", "") or "")
            return (
                f"send {len(text)} chars to session `{args.get('session_id', '')}` "
                f"(append_newline={bool(args.get('append_newline', True))})"
            )
        if tool_name == "process_stop":
            return (
                f"stop session `{args.get('session_id', '')}` "
                f"(force={bool(args.get('force', False))})"
            )
        if tool_name == "process_kill":
            target = args.get("pid") or args.get("name") or "unknown"
            return f"terminate process `{target}`"
        if tool_name == "write_file":
            content = str(args.get("content", "") or "")
            return (
                f"write {len(content)} chars to `{args.get('path', '')}` "
                f"(backup={bool(args.get('backup', True))})"
            )
        if tool_name == "edit_file":
            return (
                f"replace text in `{args.get('path', '')}` "
                f"(old_len={len(str(args.get('old_string', '') or ''))}, "
                f"new_len={len(str(args.get('new_string', '') or ''))}, "
                f"replace_all={bool(args.get('replace_all', False))})"
            )
        if tool_name == "edit_lines":
            content = str(args.get("content", "") or "")
            end_line = args.get("end_line") or args.get("line")
            mode = "insert before" if args.get("insert") else "replace"
            return (
                f"{mode} lines {args.get('line', '')}-{end_line} in `{args.get('path', '')}` "
                f"with {len(content)} chars"
            )
        if tool_name == "append_file":
            content = str(args.get("content", "") or "")
            return f"append {len(content)} chars to `{args.get('path', '')}`"
        return f"call {tool_name}({json.dumps(args, ensure_ascii=False, sort_keys=True)[:160]})"

    @staticmethod
    def _is_dry_run(args: dict) -> bool:
        if not isinstance(args, dict):
            return False
        value = args.get("dry_run", False)
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


    def _classify_exception(self, e: Exception) -> ErrorType:
        """Classifica eccezione in ErrorType."""
        name = type(e).__name__.lower()
        msg = str(e).lower()

        if "timeout" in name or "timeout" in msg:
            return ErrorType.TIMEOUT
        if "permission" in name or "permission" in msg or "access denied" in msg:
            return ErrorType.PERMISSION
        if "not found" in msg or "no such file" in msg or "filenotfound" in name:
            return ErrorType.NOT_FOUND
        if "connection" in name or "network" in msg or "urlopen" in msg:
            return ErrorType.NETWORK
        return ErrorType.RUNTIME

    def _validate_params(self, tool: Tool, args: dict) -> Optional[str]:
        """Validazione base dei parametri."""
        if not tool.parameters:
            return None
        required = tool.parameters.get("required", [])
        for param in required:
            if param not in args:
                return f"Parametro obbligatorio mancante: {param}"
        return None

    def _describe_action(self, tool_name: str, args: dict) -> str:
        """Breve descrizione dell'azione per conferma utente."""
        if tool_name == "shell":
            return args.get("command", "?")[:100]
        return f"{tool_name}({json.dumps(args, ensure_ascii=False)[:100]})"

    def _audit_policy_decision(self, tool_name: str, args: dict, decision,
                               action: AuditAction, actor: str, source: str,
                               success: bool, note: str = "") -> None:
        if not self.audit_log:
            return

        try:
            details = {
                "policy_action": decision.action.value,
                "reason": decision.reason,
                "risk": decision.risk.value,
                "note": note,
                "args": self._args_summary(args),
            }
            self.audit_log.log(
                action,
                actor=actor,
                target=tool_name,
                details=json.dumps(details, ensure_ascii=False, sort_keys=True),
                risk_level=self._audit_risk(decision.risk),
                success=success,
                source=source,
            )
        except Exception:
            pass

    def _audit_tool_result(self, tool_name: str, args: dict, result: ToolResult,
                           actor: str, source: str, decision=None) -> None:
        if not self.audit_log:
            return

        try:
            details = {
                "args": self._args_summary(args),
                "duration_ms": result.duration_ms,
                "error_type": result.error_type.value,
            }
            if self._is_dry_run(args):
                details["dry_run"] = True
            risk_level = "low"
            if decision is not None:
                details["policy_action"] = decision.action.value
                details["risk"] = decision.risk.value
                risk_level = self._audit_risk(decision.risk)
            if result.error:
                details["error"] = self._preview_value(result.error)

            action = AuditAction.SHELL_EXEC if tool_name == "shell" else AuditAction.TOOL_CALL
            target = tool_name
            if tool_name == "shell" and isinstance(args, dict):
                target = str(args.get("command", "") or "shell")[:200]

            self.audit_log.log(
                action,
                actor=actor,
                target=target,
                details=json.dumps(details, ensure_ascii=False, sort_keys=True),
                risk_level=risk_level,
                success=result.success,
                source=source,
            )
        except Exception:
            pass

    def _args_summary(self, args: Any) -> str:
        if not isinstance(args, dict):
            return self._preview_value(args)

        summary: dict[str, str] = {}
        for key, value in args.items():
            summary[str(key)] = self._preview_value(value)

        try:
            return json.dumps(summary, ensure_ascii=False, sort_keys=True)[:400]
        except TypeError:
            return str(summary)[:400]

    def _preview_value(self, value: Any) -> str:
        if isinstance(value, str):
            compact = value.replace("\r", "\\r").replace("\n", "\\n")
            return compact[:157] + "..." if len(compact) > 160 else compact
        if isinstance(value, bytes):
            return f"<bytes len={len(value)}>"
        if isinstance(value, dict):
            keys = ", ".join(str(k) for k in list(value.keys())[:8])
            suffix = ", ..." if len(value) > 8 else ""
            return f"<dict len={len(value)} keys=[{keys}{suffix}]>"
        if isinstance(value, (list, tuple, set)):
            return f"<{type(value).__name__} len={len(value)}>"
        return str(value)[:160]

    def _audit_risk(self, risk) -> str:
        value = getattr(risk, "value", str(risk))
        return {
            "safe": "low",
            "moderate": "medium",
            "high": "high",
            "critical": "critical",
        }.get(value, "low")
