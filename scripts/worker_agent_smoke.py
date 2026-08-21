"""Exercise a real Redis queue job through the scheduled Agent Runtime path."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from uuid import uuid4

from redis import Redis

from packages.agent_runtime import RunStatus
from packages.scheduler import RedisJobQueue, Scheduler, load_schedule
from services.bootstrap import build_runtime
from services.worker.executor import (
    AgentScheduleExecutor,
    build_schedule_job_payload,
    build_worker_identity,
    validate_schedule_job_payload,
)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def main() -> int:
    redis_url = os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")
    definition = load_schedule("schedules/order-status-demo.yaml")
    scheduled_at = datetime(2030, 1, 1, 2, 0, tzinfo=UTC)
    client = Redis.from_url(redis_url, socket_timeout=5, socket_connect_timeout=5)
    stream = f"agent:worker-smoke:{uuid4().hex}"
    group = f"worker-smoke:{uuid4().hex}"
    producer = RedisJobQueue(client, stream=stream, group=group, consumer="producer")
    consumer = RedisJobQueue(client, stream=stream, group=group, consumer="consumer")
    try:
        _require(bool(client.ping()), "Redis did not respond to PING")
        queued = producer.enqueue(build_schedule_job_payload(definition, scheduled_at))
        received = consumer.receive(block_seconds=2)
        _require(received is not None, "worker did not receive the scheduled Agent job")
        queued_at = validate_schedule_job_payload(received.payload, definition)
        scheduled = definition.scheduled_for(queued_at)
        _require(scheduled is not None, "scheduled Agent fixture is not due")
        run_key = f"{definition.id}:{scheduled.astimezone(UTC).isoformat()}"
        claim = consumer.claim_run(run_key, lease_seconds=300)
        _require(claim.status == "claimed" and claim.token is not None, "run was not claimed")
        runtime, _audit = build_runtime()
        executor = AgentScheduleExecutor(runtime, build_worker_identity("development"))
        scheduler = Scheduler()
        scheduler.register(definition)
        run = scheduler.trigger(definition.id, queued_at, executor.execute)
        _require(run.status is RunStatus.SUCCEEDED, "scheduled Agent did not succeed")
        _require(run.attempts == 1, "scheduled Agent unexpectedly retried")
        _require(received.id == queued.id, "queue changed the scheduled job identity")
        consumer.complete_run(run.idempotency_key, claim.token)
        consumer.ack(received)
        duplicate = producer.claim_run(run.idempotency_key)
        _require(duplicate.status == "completed", "completed run was not deduplicated")
        pending = client.xpending(stream, group)
        _require(isinstance(pending, dict) and pending.get("pending") == 0, "job remains pending")
        print(f"worker-agent-smoke: PASS run={run.idempotency_key} status={run.status.value}")
        return 0
    finally:
        client.delete(stream)
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
