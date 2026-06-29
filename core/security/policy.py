"""
openvurp Security — Tool Policy Engine

Unifica la decisione prima dell'esecuzione di un tool:
- allow: esegui subito
- require_approval: chiedi conferma esplicita
- block: non eseguire
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from core.safety import ActionRisk, SafetyGuard
from core.tools import Tool


class PolicyAction(Enum):
    ALLOW = "allow"
    REQUIRE_APPROVAL = "require_approval"
    BLOCK = "block"


@dataclass(frozen=True)
class PolicyDecision:
    action: PolicyAction
    reason: str = ""
    risk: ActionRisk = ActionRisk.SAFE

    @property
    def allowed_without_prompt(self) -> bool:
        return self.action == PolicyAction.ALLOW

    @property
    def needs_approval(self) -> bool:
        return self.action == PolicyAction.REQUIRE_APPROVAL

    @property
    def blocked(self) -> bool:
        return self.action == PolicyAction.BLOCK


class ToolPolicyEngine:
    """Decide se un tool puo' partire, deve chiedere conferma o va bloccato."""

    def __init__(self, safety: SafetyGuard | None = None):
        self.safety = safety

    def evaluate(self, tool: Tool, args: dict) -> PolicyDecision:
        if not self.safety:
            if tool.requires_approval:
                return PolicyDecision(
                    PolicyAction.REQUIRE_APPROVAL,
                    "Il tool richiede approvazione.",
                )
            return PolicyDecision(PolicyAction.ALLOW)

        risk = self._risk_for_tool(tool.name, args)
        ok, reason = self.safety.check_tool(tool.name, args)

        if not ok:
            if risk == ActionRisk.CRITICAL or self._is_hard_block_reason(reason):
                return PolicyDecision(PolicyAction.BLOCK, reason, risk)

            if tool.requires_approval or self._is_approval_reason(reason):
                return PolicyDecision(
                    PolicyAction.REQUIRE_APPROVAL,
                    reason or "Azione sensibile.",
                    risk,
                )

            return PolicyDecision(PolicyAction.BLOCK, reason, risk)

        if tool.requires_approval:
            return PolicyDecision(
                PolicyAction.REQUIRE_APPROVAL,
                "Il tool richiede approvazione.",
                risk,
            )

        if risk == ActionRisk.HIGH:
            return PolicyDecision(
                PolicyAction.REQUIRE_APPROVAL,
                "Azione ad alto rischio.",
                risk,
            )

        if risk == ActionRisk.CRITICAL:
            return PolicyDecision(
                PolicyAction.BLOCK,
                "Azione critica bloccata.",
                risk,
            )

        return PolicyDecision(PolicyAction.ALLOW, reason, risk)

    def _risk_for_tool(self, tool_name: str, args: dict) -> ActionRisk:
        if not self.safety:
            return ActionRisk.SAFE
        if tool_name == "shell":
            return self.safety.classify(str(args.get("command", "") or ""))
        if tool_name == "process_start":
            return self.safety.classify(str(args.get("command", "") or ""))
        if tool_name == "process_write":
            return self.safety.classify(str(args.get("text", "") or ""))
        return ActionRisk.MODERATE if tool_name else ActionRisk.SAFE

    @staticmethod
    def _is_approval_reason(reason: str) -> bool:
        text = (reason or "").lower()
        return any(marker in text for marker in (
            "richiede approvazione",
            "file critico",
            "fuori dal workspace",
            "risk=high",
        ))

    @staticmethod
    def _is_hard_block_reason(reason: str) -> bool:
        text = (reason or "").lower()
        return any(marker in text for marker in (
            "critico bloccato",
            "input critico bloccato",
            "comando critico bloccato",
        ))
