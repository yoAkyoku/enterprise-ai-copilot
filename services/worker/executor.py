"""Trusted scheduled-Agent execution for the Redis worker boundary."""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Mapping
from collections.abc import Callable
from datetime import datetime

from packages.agent_runtime import AgentRuntime, IdentityContext, RunStatus
from packages.scheduler import ScheduleDefinition
from services.bootstrap import demo_identity

_SAFE_IDENTITY_VALUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,255}$")
_WORKER_ROLES = {"support", "sales", "manager", "admin"}


class WorkerConfigurationError(ValueError):
    """Raised when a worker cannot establish a trusted execution context."""


class WorkerExecutionError(RuntimeError):
    """Raised when the Agent does not reach a verified terminal success."""


def build_worker_identity(platform_env: str | None = None) -> IdentityContext:
    """Build identity from deployment configuration, never from a queue job."""

    resolved_env = (platform_env or os.getenv("AGENT_PLATFORM_ENV", "development")).lower()
    if resolved_env not in {"staging", "production"}:
        return demo_identity()
    values = {
        "user_id": os.getenv("AGENT_WORKER_USER_ID", "").strip(),
        "workspace_id": os.getenv("AGENT_WORKER_WORKSPACE_ID", "").strip(),
        "tenant_id": os.getenv("AGENT_WORKER_TENANT_ID", "").strip(),
        "role": os.getenv("AGENT_WORKER_ROLE", "").strip().lower(),
    }
    if any(not value for value in values.values()):
        raise WorkerConfigurationError(
            "staging and production require AGENT_WORKER_USER_ID, "
            "AGENT_WORKER_WORKSPACE_ID, AGENT_WORKER_TENANT_ID and AGENT_WORKER_ROLE"
        )
    if any(not _SAFE_IDENTITY_VALUE.fullmatch(value) for value in values.values()):
        raise WorkerConfigurationError("worker identity contains an invalid value")
    if values["role"] not in _WORKER_ROLES:
        raise WorkerConfigurationError("AGENT_WORKER_ROLE is not allowed for scheduled runs")
    return IdentityContext(**values)


def validate_agent_schedule(schedule: ScheduleDefinition) -> None:
    """Require task inputs before a queue job can execute an Agent."""

    if schedule.agent != AgentRuntime.agent_id:
        raise WorkerConfigurationError("scheduled Agent is not registered in this worker image")
    if not isinstance(schedule.query, str) or not schedule.query.strip():
        raise WorkerConfigurationError("scheduled Agent requires run.query")
    if not isinstance(schedule.order_id, str) or not schedule.order_id.strip():
        raise WorkerConfigurationError("customer-service-agent requires run.order_id")


def build_schedule_job_payload(
    schedule: ScheduleDefinition, scheduled_at: datetime
) -> dict[str, object]:
    """Serialize only the reviewed schedule inputs into a queue job."""

    if scheduled_at.tzinfo is None:
        raise WorkerConfigurationError("scheduled job timestamp must include a timezone")
    return {
        "kind": "scheduled_agent",
        "schedule_id": schedule.id,
        "schedule_version": schedule.version,
        "agent": schedule.agent,
        "scheduled_at": scheduled_at.isoformat(),
        "permissions_mode": schedule.permissions_mode,
        "query": schedule.query,
        "order_id": schedule.order_id,
        "allow_external_processing": schedule.allow_external_processing,
    }


def validate_schedule_job_payload(
    payload: Mapping[str, object], schedule: ScheduleDefinition
) -> datetime:
    """Reject stale, expanded or malformed queue payloads before execution."""

    expected = {
        "kind": "scheduled_agent",
        "schedule_id": schedule.id,
        "schedule_version": schedule.version,
        "agent": schedule.agent,
        "permissions_mode": schedule.permissions_mode,
        "query": schedule.query,
        "order_id": schedule.order_id,
        "allow_external_processing": schedule.allow_external_processing,
    }
    expected_keys = set(expected) | {"scheduled_at"}
    if set(payload) != expected_keys or any(
        payload.get(key) != value for key, value in expected.items()
    ):
        raise WorkerConfigurationError("queued job does not match the reviewed schedule definition")
    scheduled_at = payload.get("scheduled_at")
    if not isinstance(scheduled_at, str) or len(scheduled_at) > 64:
        raise WorkerConfigurationError("queued job scheduled_at is invalid")
    try:
        queued_at = datetime.fromisoformat(scheduled_at)
    except ValueError as exc:
        raise WorkerConfigurationError("queued job scheduled_at is invalid") from exc
    if queued_at.tzinfo is None:
        raise WorkerConfigurationError("queued job scheduled_at must include a timezone")
    return queued_at


class AgentScheduleExecutor:
    """Adapt one typed Agent Runtime to the scheduler executor contract."""

    def __init__(
        self,
        runtime: AgentRuntime,
        identity: IdentityContext,
        cancel_checker: Callable[[], bool] | None = None,
    ) -> None:
        self._runtime = runtime
        self._identity = identity
        self._cancel_checker = cancel_checker

    def execute(self, schedule: ScheduleDefinition, idempotency_key: str) -> str:
        validate_agent_schedule(schedule)
        digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
        result = self._runtime.run(
            schedule.query or "",
            self._identity,
            order_id=schedule.order_id,
            request_id=f"schedule-request-{digest[:32]}",
            trace_id=f"schedule-trace-{digest[:32]}",
            allow_external_model_processing=schedule.allow_external_processing,
            cancel_checker=self._cancel_checker,
        )
        if result.status is not RunStatus.SUCCEEDED:
            raise WorkerExecutionError("scheduled Agent did not produce verified success")
        return result.message
