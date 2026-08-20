"""Typed contracts for the first Agent Runtime slice.

The contracts intentionally use only the Python standard library so the first
vertical slice can be tested from a clean checkout without external services.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class ToolRisk(StrEnum):
    READ = "read"
    WRITE = "write"
    EXTERNAL_SEND = "external_send"
    DESTRUCTIVE = "destructive"


class RunStatus(StrEnum):
    SUCCEEDED = "succeeded"
    PARTIAL_SUCCESS = "partial_success"
    BLOCKED = "blocked"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class IdentityContext:
    user_id: str
    workspace_id: str
    tenant_id: str
    role: str


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    risk: ToolRisk
    description: str


@dataclass(frozen=True)
class ToolCallRequest:
    request_id: str
    trace_id: str
    run_id: str
    identity: IdentityContext
    tool_name: str
    arguments: dict[str, Any]
    idempotency_key: str


@dataclass(frozen=True)
class ToolResult:
    success: bool
    data: dict[str, Any] = field(default_factory=dict)
    source_id: str | None = None
    observed_at: str | None = None
    external_ref: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class PolicyDecision:
    outcome: str
    reason: str
    tool_name: str
    risk: ToolRisk | None = None


@dataclass(frozen=True)
class AuditEvent:
    event_type: str
    request_id: str
    trace_id: str
    run_id: str
    workspace_id: str
    agent_id: str
    payload: dict[str, Any]
    created_at: str = field(default_factory=utc_now)


@dataclass(frozen=True)
class RunResult:
    status: RunStatus
    run_id: str
    trace_id: str
    agent_id: str
    message: str
    source_id: str | None = None
    observed_at: str | None = None
    external_ref: str | None = None
