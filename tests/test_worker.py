from __future__ import annotations

import os
import unittest
from datetime import UTC, datetime
from unittest.mock import patch

from packages.agent_runtime import IdentityContext
from packages.scheduler import RedisJobQueue, ScheduleDefinition
from services.bootstrap import build_runtime
from services.worker.executor import (
    AgentScheduleExecutor,
    WorkerConfigurationError,
    build_worker_identity,
    validate_agent_schedule,
)


class WorkerExecutionTests(unittest.TestCase):
    @staticmethod
    def definition(**overrides: object) -> ScheduleDefinition:
        values: dict[str, object] = {
            "id": "worker-schedule",
            "version": "0.1.0",
            "agent": "customer-service-agent",
            "schedule_type": "one_shot",
            "at": "2030-01-01T09:00:00+08:00",
            "timezone_name": "Asia/Taipei",
            "query": "Provide the verified order status.",
            "order_id": "SO-1001",
        }
        values.update(overrides)
        return ScheduleDefinition(**values)  # type: ignore[arg-type]

    def test_agent_executor_runs_real_runtime_with_configured_scope(self) -> None:
        runtime, audit = build_runtime()
        identity = IdentityContext(
            user_id="worker-user",
            workspace_id="demo-workspace",
            tenant_id="demo-tenant",
            role="support",
        )
        message = AgentScheduleExecutor(runtime, identity).execute(
            self.definition(), "worker-schedule:2030-01-01T01:00:00+00:00"
        )
        self.assertIn("Order SO-1001 is in_transit", message)
        self.assertEqual(audit.events[-1].event_type, "run.succeeded")
        self.assertEqual(audit.events[-1].workspace_id, "demo-workspace")

    def test_production_worker_identity_never_defaults_to_demo_scope(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(WorkerConfigurationError):
                build_worker_identity("production")

    def test_production_worker_identity_requires_allowed_role_and_scope(self) -> None:
        values = {
            "AGENT_WORKER_USER_ID": "worker-user",
            "AGENT_WORKER_WORKSPACE_ID": "workspace-a",
            "AGENT_WORKER_TENANT_ID": "tenant-a",
            "AGENT_WORKER_ROLE": "customer",
        }
        with patch.dict(os.environ, values, clear=True):
            with self.assertRaises(WorkerConfigurationError):
                build_worker_identity("production")
        values["AGENT_WORKER_ROLE"] = "support"
        with patch.dict(os.environ, values, clear=True):
            identity = build_worker_identity("production")
        self.assertEqual(identity.tenant_id, "tenant-a")
        self.assertEqual(identity.role, "support")

    def test_agent_schedule_requires_reviewed_task_inputs(self) -> None:
        with self.assertRaises(WorkerConfigurationError):
            validate_agent_schedule(self.definition(query=None))
        with self.assertRaises(WorkerConfigurationError):
            validate_agent_schedule(self.definition(agent="unknown-agent"))

    def test_schedule_payload_requires_timezone(self) -> None:
        from services.worker.executor import (
            build_schedule_job_payload,
            validate_schedule_job_payload,
        )

        with self.assertRaises(WorkerConfigurationError):
            build_schedule_job_payload(self.definition(), datetime(2030, 1, 1))
        payload = build_schedule_job_payload(self.definition(), datetime(2030, 1, 1, tzinfo=UTC))
        payload["scheduled_at"] = "2030-01-01T01:00:00"
        with self.assertRaises(WorkerConfigurationError):
            validate_schedule_job_payload(payload, self.definition())

    def test_redis_run_claim_deduplicates_after_terminal_completion(self) -> None:
        class FakeRedis:
            def __init__(self) -> None:
                self.values: dict[str, str] = {}

            def get(self, key: str) -> str | None:
                return self.values.get(key)

            def set(self, key: str, value: str, *, nx: bool, ex: int) -> bool:
                del ex
                if nx and key in self.values:
                    return False
                self.values[key] = value
                return True

            def eval(
                self, _script: str, _key_count: int, key: str, token: str, _retention: int
            ) -> int:
                if self.values.get(key) != token:
                    return 0
                self.values[key] = "completed"
                return 1

        queue = RedisJobQueue(FakeRedis(), stream="worker-test")
        first = queue.claim_run("worker-schedule:slot")
        self.assertEqual(first.status, "claimed")
        self.assertEqual(queue.claim_run("worker-schedule:slot").status, "in_progress")
        queue.complete_run("worker-schedule:slot", first.token or "")
        self.assertEqual(queue.claim_run("worker-schedule:slot").status, "completed")


if __name__ == "__main__":
    unittest.main()
