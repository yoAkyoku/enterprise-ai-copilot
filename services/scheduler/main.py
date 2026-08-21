"""Durable Redis schedule producer.

The API/worker processes do not infer schedules from model output. This small
producer polls reviewed schedule definitions and uses a Redis idempotency key
for each due slot, so restart or multiple producer replicas cannot enqueue the
same slot repeatedly.
"""

from __future__ import annotations

import argparse
import time
from datetime import UTC, datetime

from packages.scheduler import RedisJobQueue, ScheduleDefinition, load_schedule
from services.worker.executor import build_schedule_job_payload, validate_agent_schedule


def enqueue_due(queue: RedisJobQueue, definition: ScheduleDefinition, now: datetime) -> bool:
    scheduled = definition.scheduled_for(now)
    if scheduled is None:
        return False
    idempotency_key = f"{definition.id}:{scheduled.astimezone(UTC).isoformat()}"
    job = queue.enqueue_once(
        build_schedule_job_payload(definition, now),
        idempotency_key,
    )
    if job is not None:
        print(f"schedule={definition.id} queued={job.id} idempotency_key={idempotency_key}")
        return True
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Produce due reviewed Agent schedules into Redis")
    parser.add_argument("schedule")
    parser.add_argument("--redis-url", required=True)
    parser.add_argument("--interval-seconds", type=int, default=15)
    parser.add_argument("--once", action="store_true", help="poll once and exit")
    args = parser.parse_args(argv)
    if args.interval_seconds <= 0 or args.interval_seconds > 300:
        parser.error("--interval-seconds must be between 1 and 300")
    definition = load_schedule(args.schedule)
    validate_agent_schedule(definition)
    from redis import Redis

    client = Redis.from_url(args.redis_url, socket_timeout=5, socket_connect_timeout=5)
    queue = RedisJobQueue(client, consumer="schedule-producer")
    try:
        while True:
            enqueue_due(queue, definition, datetime.now(UTC))
            if args.once:
                return 0
            time.sleep(args.interval_seconds)
    except KeyboardInterrupt:
        print("schedule producer stopped gracefully")
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
