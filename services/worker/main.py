"""Deterministic schedule validation and dry-run worker command."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime

from packages.scheduler import RedisJobQueue, Scheduler, load_schedule


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a safe synthetic schedule dry-run")
    parser.add_argument("schedule")
    parser.add_argument("--at", help="UTC ISO timestamp used as the trigger time")
    parser.add_argument("--redis-url", help="Redis URL for distributed enqueue/consume mode")
    parser.add_argument(
        "--queue-mode", choices=("dry-run", "enqueue", "consume"), default="dry-run"
    )
    parser.add_argument("--worker-id", help="Redis Streams consumer name")
    parser.add_argument("--block-seconds", type=int, default=5)
    args = parser.parse_args()
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
                job = queue.enqueue(
                    {
                        "schedule_id": definition.id,
                        "agent": definition.agent,
                        "scheduled_at": now.isoformat(),
                        "permissions_mode": definition.permissions_mode,
                    }
                )
                print(f"queued job={job.id} schedule={definition.id}")
                return 0
            job = queue.receive(block_seconds=args.block_seconds)
            if job is None:
                print("no queued job")
                return 0
            payload = job.payload
            if payload.get("schedule_id") != definition.id:
                raise ValueError("queued job schedule does not match the supplied definition")
            queued_at = datetime.fromisoformat(str(payload["scheduled_at"]))
            if queued_at.tzinfo is None:
                queued_at = queued_at.replace(tzinfo=UTC)
            run = scheduler.trigger(
                definition.id,
                queued_at,
                lambda schedule, key: (
                    f"distributed dry-run agent={schedule.agent} idempotency_key={key}"
                ),
            )
            queue.ack(job)
            print(
                f"schedule={run.schedule_id} status={run.status} attempts={run.attempts} "
                f"idempotency_key={run.idempotency_key} message={run.message}"
            )
            return 0 if run.status.value in {"succeeded", "skipped", "paused", "expired"} else 1
        except (ImportError, OSError, ValueError, RuntimeError) as exc:
            print(f"queue worker failed: {exc}")
            return 1
    run = scheduler.trigger(
        definition.id,
        now,
        lambda schedule, key: (
            f"dry-run agent={schedule.agent} idempotency_key={key} permissions={schedule.permissions_mode}"
        ),
    )
    print(
        f"schedule={run.schedule_id} status={run.status} attempts={run.attempts} "
        f"idempotency_key={run.idempotency_key} message={run.message}"
    )
    return 0 if run.status.value in {"succeeded", "skipped", "paused", "expired"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
