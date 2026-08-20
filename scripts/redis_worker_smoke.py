"""Exercise the Redis Streams worker adapter against a real Redis instance.

This smoke intentionally uses a unique stream and consumer group so it can run
in an isolated CI service without deleting another tenant's data. It proves
queue identity, acknowledgement, reconnect-safe consumption and abandoned-job
reclaim; it does not claim a full production worker deployment.
"""

from __future__ import annotations

import os
import time
from uuid import uuid4

from redis import Redis

from packages.scheduler import RedisJobQueue


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def main() -> int:
    redis_url = os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")
    client = Redis.from_url(redis_url, socket_timeout=5, socket_connect_timeout=5)
    stream = f"agent:smoke:{uuid4().hex}"
    group = f"smoke-workers-{uuid4().hex}"
    worker_a = None
    worker_b = None
    try:
        _require(bool(client.ping()), "Redis did not respond to PING")
        producer = RedisJobQueue(
            client,
            stream=stream,
            group=group,
            consumer="producer",
            claim_after_seconds=1,
        )
        worker_a = RedisJobQueue(
            client,
            stream=stream,
            group=group,
            consumer="worker-a",
            claim_after_seconds=1,
        )

        trace_id = f"trace-smoke-{uuid4().hex}"
        acknowledged = producer.enqueue({"kind": "normal", "trace_id": trace_id})
        received = worker_a.receive(block_seconds=2)
        _require(received is not None, "worker did not receive the queued job")
        _require(received.id == acknowledged.id, "queue changed the job identity")
        _require(received.payload.get("trace_id") == trace_id, "trace identity was not preserved")
        worker_a.ack(received)

        abandoned = producer.enqueue({"kind": "abandoned", "trace_id": trace_id})
        first_delivery = worker_a.receive(block_seconds=2)
        _require(first_delivery is not None, "worker did not receive the reclaim fixture")
        _require(first_delivery.id == abandoned.id, "reclaim fixture identity changed")

        # A second client represents a restarted worker process. Wait until the
        # configured idle threshold is crossed before asking it to reclaim.
        time.sleep(1.25)
        reconnected = Redis.from_url(redis_url, socket_timeout=5, socket_connect_timeout=5)
        _require(bool(reconnected.ping()), "reconnected worker client did not respond")
        worker_b = RedisJobQueue(
            reconnected,
            stream=stream,
            group=group,
            consumer="worker-b",
            claim_after_seconds=1,
        )
        reclaimed = worker_b.receive(block_seconds=2)
        _require(reclaimed is not None, "restarted worker did not reclaim the abandoned job")
        _require(reclaimed.id == abandoned.id, "reclaimed job identity changed")
        _require(reclaimed.payload.get("trace_id") == trace_id, "reclaimed trace identity changed")
        worker_b.ack(reclaimed)

        pending = client.xpending(stream, group)
        _require(isinstance(pending, dict) and pending.get("pending") == 0, "jobs remain pending")
        print(
            "redis-worker-smoke: PASS "
            f"stream={stream} acknowledged={acknowledged.id} reclaimed={reclaimed.id}"
        )
        return 0
    finally:
        client.delete(stream)
        if worker_b is not None:
            worker_b.client.close()  # type: ignore[attr-defined]
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
