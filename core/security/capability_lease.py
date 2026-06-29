"""
openvurp Security - Temporary capability leases.

Capability leases let the owner approve a narrow class of repeated actions for
a short time without turning off the permission system. They are intentionally
bounded by actor, source, tool, risk level, TTL, use count, and optional command
or path prefixes.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
import os
import time
import uuid
from typing import Any

from core.security.audit import redact


RISK_RANK = {
    "safe": 0,
    "moderate": 1,
    "high": 2,
    "critical": 3,
}


@dataclass
class CapabilityLease:
    id: str
    actor: str
    source: str
    tool_name: str
    risk: str = "high"
    reason: str = ""
    created_at: float = field(default_factory=time.time)
    expires_at: float = 0.0
    max_uses: int = 5
    uses: int = 0
    command_prefix: str = ""
    path_prefix: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def expired(self) -> bool:
        return bool(self.expires_at and time.time() > self.expires_at)

    @property
    def exhausted(self) -> bool:
        return self.max_uses > 0 and self.uses >= self.max_uses

    @property
    def active(self) -> bool:
        return not self.expired and not self.exhausted

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_public_dict(self) -> dict[str, Any]:
        data = self.to_dict()
        for key in ("reason", "command_prefix", "path_prefix"):
            data[key] = redact(str(data.get(key, "") or ""))
        data["active"] = self.active
        data["remaining_uses"] = (
            None if self.max_uses <= 0 else max(0, self.max_uses - self.uses)
        )
        data["seconds_left"] = max(0, int(self.expires_at - time.time())) if self.expires_at else None
        return data


class CapabilityLeaseManager:
    """JSON-backed manager for narrow, temporary approval leases."""

    LEASE_FILE = "capability_leases.json"

    def __init__(self, memory_dir: str):
        self.memory_dir = memory_dir
        self.path = os.path.join(memory_dir, self.LEASE_FILE)
        os.makedirs(memory_dir, exist_ok=True)

    def grant(
        self,
        actor: str,
        source: str,
        tool_name: str,
        risk: str = "high",
        ttl_seconds: int = 600,
        max_uses: int = 5,
        reason: str = "",
        command_prefix: str = "",
        path_prefix: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> CapabilityLease:
        tool_name = (tool_name or "").strip()
        if not tool_name:
            raise ValueError("tool_name is required")

        risk = self._normalize_risk(risk)
        if risk == "critical":
            raise ValueError("Critical actions cannot be leased")

        ttl_seconds = max(1, min(int(ttl_seconds or 600), 86_400))
        max_uses = max(0, min(int(max_uses or 0), 100))
        now = time.time()
        lease = CapabilityLease(
            id=uuid.uuid4().hex[:12],
            actor=(actor or "agent").strip() or "agent",
            source=(source or "cli").strip() or "cli",
            tool_name=tool_name,
            risk=risk,
            reason=reason or "",
            created_at=now,
            expires_at=now + ttl_seconds,
            max_uses=max_uses,
            uses=0,
            command_prefix=(command_prefix or "").strip(),
            path_prefix=(path_prefix or "").strip(),
            metadata=metadata or {},
        )

        rows = self._load()
        rows.append(lease)
        self._save(rows)
        return lease

    def find_valid(
        self,
        actor: str,
        source: str,
        tool_name: str,
        args: dict[str, Any] | None,
        risk: str,
    ) -> CapabilityLease | None:
        risk = self._normalize_risk(risk)
        if risk == "critical":
            return None

        for lease in self._load():
            if not lease.active:
                continue
            if not self._matches_scope(lease.actor, actor):
                continue
            if not self._matches_scope(lease.source, source):
                continue
            if not self._matches_scope(lease.tool_name, tool_name):
                continue
            if RISK_RANK[risk] > RISK_RANK[lease.risk]:
                continue
            if not self._matches_args(lease, args or {}):
                continue
            return lease
        return None

    def consume(self, lease_id: str) -> CapabilityLease | None:
        rows = self._load()
        for lease in rows:
            if lease.id == lease_id:
                if not lease.active:
                    return None
                lease.uses += 1
                self._save(rows)
                return lease
        return None

    def revoke(self, lease_id: str) -> bool:
        rows = self._load()
        kept = [lease for lease in rows if lease.id != lease_id]
        if len(kept) == len(rows):
            return False
        self._save(kept)
        return True

    def list_leases(self, include_expired: bool = False) -> list[CapabilityLease]:
        rows = self._load()
        if include_expired:
            return rows
        return [lease for lease in rows if lease.active]

    def prune(self) -> int:
        rows = self._load()
        kept = [lease for lease in rows if lease.active]
        removed = len(rows) - len(kept)
        if removed:
            self._save(kept)
        return removed

    def _load(self) -> list[CapabilityLease]:
        if not os.path.exists(self.path):
            return []
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
        except Exception:
            return []

        leases: list[CapabilityLease] = []
        for item in raw if isinstance(raw, list) else []:
            if not isinstance(item, dict):
                continue
            try:
                leases.append(CapabilityLease(**item))
            except TypeError:
                continue
        return leases

    def _save(self, leases: list[CapabilityLease]) -> None:
        os.makedirs(self.memory_dir, exist_ok=True)
        tmp = f"{self.path}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump([lease.to_dict() for lease in leases], handle, indent=2, ensure_ascii=False)
        os.replace(tmp, self.path)

    def _matches_args(self, lease: CapabilityLease, args: dict[str, Any]) -> bool:
        if lease.command_prefix:
            command = str(args.get("command", "") or args.get("text", "") or "").strip()
            if not command.startswith(lease.command_prefix):
                return False

        if lease.path_prefix:
            raw_path = str(args.get("path", "") or "").strip()
            if not raw_path:
                return False
            if not self._path_matches_prefix(raw_path, lease.path_prefix):
                return False

        return True

    @staticmethod
    def _path_matches_prefix(path: str, prefix: str) -> bool:
        path = CapabilityLeaseManager._normalize_path(path)
        prefix = CapabilityLeaseManager._normalize_path(prefix)
        if not path or not prefix:
            return False
        try:
            path_abs = os.path.realpath(os.path.abspath(os.path.expanduser(path)))
            prefix_abs = os.path.realpath(os.path.abspath(os.path.expanduser(prefix)))
            return path_abs == prefix_abs or path_abs.startswith(prefix_abs + os.sep)
        except Exception:
            path_norm = path.replace("\\", "/")
            prefix_norm = prefix.replace("\\", "/").rstrip("/")
            return path_norm == prefix_norm or path_norm.startswith(prefix_norm + "/")

    @staticmethod
    def _normalize_path(path: str) -> str:
        path = (path or "").strip().strip('"').strip("'")
        if len(path) >= 3 and path[1] == ":" and path[2] in ("/", "\\"):
            drive = path[0].lower()
            rest = path[3:].replace("\\", "/")
            return f"/mnt/{drive}/{rest}"
        return path

    @staticmethod
    def _matches_scope(lease_value: str, current_value: str) -> bool:
        lease_value = (lease_value or "").strip()
        current_value = (current_value or "").strip()
        return lease_value == "*" or lease_value == current_value

    @staticmethod
    def _normalize_risk(risk: str) -> str:
        risk = str(risk or "high").strip().lower()
        return risk if risk in RISK_RANK else "high"
