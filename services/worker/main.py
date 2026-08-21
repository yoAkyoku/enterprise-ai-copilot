"""Redis-backed scheduled Agent worker and developer dry-run command."""

from __future__ import annotations

import argparse
import os
from datetime import UTC, datetime

from packages.scheduler import RedisJobQueue, ScheduleDefinition, Scheduler, load_schedule
from services.bootstrap import build_runtime, build_trace_exporter
from services.worker.executor import (
    AgentScheduleExecutor,
    build_schedule_job_payload,
    build_worker_identity,
    validate_agent_schedule,
    validate_schedule_job_payload,
)


def _distributed_dry_run(schedule: ScheduleDefinition, idempotency_key: str) -> str:
    return f"distributed dry-run agent={schedule.agent} idempotency_key={idempotency_key}"


def _preview_dry_run(schedule: ScheduleDefinition, idempotency_key: str) -> str:
    return (
        f"dry-run agent={schedule.agent} idempotency_key={idempotency_key} "
        f"permissions={schedule.permissions_mode}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a policy-checked scheduled Agent")
    parser.add_argument("schedule")
    parser.add_argument("--at", help="UTC ISO timestamp used as the trigger time")
    parser.add_argument("--redis-url", help="Redis URL for distributed enqueue/consume mode")
    parser.add_argument(
        "--queue-mode", choices=("dry-run", "enqueue", "consume"), default="dry-run"
    )
    parser.add_argument("--worker-id", help="Redis Streams consumer name")
    parser.add_argument("--block-seconds", type=int, default=5)
    parser.add_argument(
        "--execution-mode",
        choices=("dry-run", "agent"),
        default=None,
        help="Execute the real Agent or only validate the queue contract",
    )
    args = parser.parse_args()
    platform_env = os.getenv("AGENT_PLATFORM_ENV", "development").lower()
    execution_mode = args.execution_mode or os.getenv("AGENT_WORKER_EXECUTION_MODE", "dry-run")
    if execution_mode not in {"dry-run", "agent"}:
        parser.error("AGENT_WORKER_EXECUTION_MODE must be dry-run or agent")
    if platform_env in {"staging", "production"} and execution_mode != "agent":
        parser.error("staging and production require --execution-mode agent")
    definition = load_schedule(args.schedule)
    scheduler = Scheduler()
    scheduler.register(definition)
    now = datetime.fromisoformat(args.at) if args.at else datetime.now(UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    if args.queue_mode != "dry-run":
        if not args.redis_url:
            parser.error("--redis-url is required for queue mode")
        try:
            from redis import Redis

            queue = RedisJobQueue(
                Redis.from_url(args.redis_url, socket_timeout=5, socket_connect_timeout=5),
                consumer=args.worker_id,
            )
            if args.queue_mode == "enqueue":
                if execution_mode == "agent":
                    validate_agent_schedule(definition)
                job = queue.enqueue(build_schedule_job_payload(definition, now))
                print(f"queued job={job.id} schedule={definition.id}")
                return 0
            job = queue.receive(block_seconds=args.block_seconds)
            if job is None:
                print("no queued job")
                return 0
            payload = job.payload
            queued_at = validate_schedule_job_payload(payload, definition)
            if execution_mode == "agent":
                runtime, _audit = build_runtime(trace_exporter=build_trace_exporter(platform_env))
                executor = AgentScheduleExecutor(runtime, build_worker_identity(platform_env))
                execute = executor.execute
            else:
                execute = _distributed_dry_run
            scheduled = definition.scheduled_for(queued_at)
            if scheduled is None:
                run = scheduler.trigger(definition.id, queued_at, execute)
            else:
                run_key = f"{definition.id}:{scheduled.astimezone(UTC).isoformat()}"
                claim = queue.claim_run(
                    run_key,
                    lease_seconds=max(60, min(definition.max_runtime_seconds + 30, 86400)),
                )
                if claim.status != "claimed":
                    queue.ack(job)
                    print(f"schedule={definition.id} status=deduplicated claim={claim.status}")
                    return 0
                if claim.token is None:
                    raise RuntimeError("Redis returned a claimed run without a token")
                run = scheduler.trigger(definition.id, queued_at, execute)
                queue.complete_run(run.idempotency_key, claim.token)
            queue.ack(job)
            message = (
                run.message
                if platform_env not in {"staging", "production"}
                else "terminal schedule state recorded"
            )
            print(
                f"schedule={run.schedule_id} status={run.status} attempts={run.attempts} "
                f"idempotency_key={run.idempotency_key} message={message}"
            )
            return 0 if run.status.value in {"succeeded", "skipped", "paused", "expired"} else 1
        except (ImportError, OSError, ValueError, RuntimeError) as exc:
            print(f"queue worker failed: {exc}")
            return 1
    if execution_mode == "agent":
        runtime, _audit = build_runtime(trace_exporter=build_trace_exporter(platform_env))
        executor = AgentScheduleExecutor(runtime, build_worker_identity(platform_env))
        execute = executor.execute
    else:
        execute = _preview_dry_run
    run = scheduler.trigger(definition.id, now, execute)
    message = (
        run.message
        if platform_env not in {"staging", "production"}
        else "terminal schedule state recorded"
    )
    print(
        f"schedule={run.schedule_id} status={run.status} attempts={run.attempts} "
        f"idempotency_key={run.idempotency_key} message={message}"
    )
    return 0 if run.status.value in {"succeeded", "skipped", "paused", "expired"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
