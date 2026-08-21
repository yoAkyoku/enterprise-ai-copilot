from __future__ import annotations

import os
import unittest
from datetime import UTC, datetime
from unittest.mock import patch

from packages.agent_runtime import IdentityContext
from packages.scheduler import Job, RedisJobQueue, ScheduleDefinition, Scheduler
from services.bootstrap import build_runtime
from services.worker.executor import (
    AgentScheduleExecutor,
    WorkerConfigurationError,
    build_worker_identity,
    validate_agent_schedule,
)
from services.worker.main import _consume_job


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
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(WorkerConfigurationError):
            build_worker_identity("production")

    def test_production_worker_identity_requires_allowed_role_and_scope(self) -> None:
        values = {
            "AGENT_WORKER_USER_ID": "worker-user",
            "AGENT_WORKER_WORKSPACE_ID": "workspace-a",
            "AGENT_WORKER_TENANT_ID": "tenant-a",
            "AGENT_WORKER_ROLE": "customer",
        }
        with (
            patch.dict(os.environ, values, clear=True),
            self.assertRaises(WorkerConfigurationError),
        ):
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
            build_schedule_job_payload(
                self.definition(), datetime.fromisoformat("2030-01-01T00:00:00")
            )
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

    def test_redis_run_cancellation_is_durable_and_not_claimable(self) -> None:
        class FakeRedis:
            def __init__(self) -> None:
                self.values: dict[str, str] = {}

            def get(self, key: str) -> str | None:
                return self.values.get(key)

            def eval(self, _script: str, _key_count: int, key: str, _retention: int) -> int:
                if self.values.get(key) in {"completed", "cancelled"}:
                    return 0
                self.values[key] = "cancelled"
                return 1

        queue = RedisJobQueue(FakeRedis(), stream="worker-cancel-test")
        self.assertTrue(queue.cancel_run("worker-schedule:cancelled"))
        self.assertFalse(queue.cancel_run("worker-schedule:cancelled"))
        self.assertTrue(queue.is_run_cancelled("worker-schedule:cancelled"))
        self.assertEqual(queue.claim_run("worker-schedule:cancelled").status, "cancelled")

    def test_worker_does_not_execute_a_cancelled_redis_job(self) -> None:
        class FakeRedis:
            def get(self, _key: str) -> str:
                return "cancelled"

        from services.worker.executor import build_schedule_job_payload

        definition = self.definition()
        queue = RedisJobQueue(FakeRedis(), stream="worker-cancel-job-test")
        scheduled_at = datetime(2030, 1, 1, 1, 0, tzinfo=UTC)
        job = Job(
            id="cancelled-job",
            payload=build_schedule_job_payload(definition, scheduled_at),
            enqueued_at="2030-01-01T01:00:00+00:00",
        )
        scheduler = Scheduler(cancel_checker=queue.is_run_cancelled)
        scheduler.register(definition)
        called = False

        def execute(_definition: ScheduleDefinition, _key: str) -> str:
            nonlocal called
            called = True
            return "must not execute"

        self.assertEqual(
            _consume_job(queue, definition, scheduler, job, execute, "development"),
            0,
        )
        self.assertFalse(called)

    def test_schedule_enqueue_claim_deduplicates_producer_restarts(self) -> None:
        class FakeRedis:
            def __init__(self) -> None:
                self.values: dict[str, str] = {}
                self.messages: list[tuple[str, dict[str, str]]] = []

            def set(self, key: str, value: str, *, nx: bool, ex: int) -> bool:
                del ex
                if nx and key in self.values:
                    return False
                self.values[key] = value
                return True

            def delete(self, key: str) -> None:
                self.values.pop(key, None)

            def xgroup_create(self, *args: object, **kwargs: object) -> None:
                del args, kwargs

            def xadd(self, _stream: str, fields: dict[str, str]) -> str:
                self.messages.append(("1-0", fields))
                return "1-0"

        client = FakeRedis()
        queue = RedisJobQueue(client, stream="worker-test")
        first = queue.enqueue_once({"kind": "scheduled_agent"}, "worker-schedule:slot")
        second = queue.enqueue_once({"kind": "scheduled_agent"}, "worker-schedule:slot")
        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertEqual(len(client.messages), 1)


if __name__ == "__main__":
    unittest.main()
