"""Issue an explicit cooperative cancellation marker for a Redis schedule run."""

from __future__ import annotations

import argparse
import json
import os

from packages.scheduler import RedisJobQueue


def cancel_run(
    redis_url: str,
    idempotency_key: str,
    *,
    retention_seconds: int = 86400,
) -> bool:
    """Persist cancellation without exposing Redis credentials or payloads."""

    if not redis_url.strip():
        raise ValueError("Redis URL is required")
    if not idempotency_key.strip():
        raise ValueError("schedule idempotency key is required")
    try:
        from redis import Redis
    except ImportError as exc:  # pragma: no cover - production extra boundary
        raise RuntimeError("redis is required for schedule cancellation") from exc
    client = Redis.from_url(redis_url, socket_timeout=5, socket_connect_timeout=5)
    queue = RedisJobQueue(
        client,
        stream=os.getenv("AGENT_REDIS_STREAM", "agent:jobs"),
        group=os.getenv("AGENT_REDIS_GROUP", "agent-workers"),
        consumer="operator-cancellation",
    )
    try:
        return queue.cancel_run(idempotency_key, retention_seconds=retention_seconds)
    finally:
        client.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Cancel one reviewed Redis schedule run")
    parser.add_argument("--redis-url", default=os.getenv("AGENT_REDIS_URL", ""))
    parser.add_argument("--idempotency-key", required=True)
    parser.add_argument("--retention-seconds", type=int, default=86400)
    parser.add_argument(
        "--confirm-cancel",
        action="store_true",
        help="acknowledge that this writes a durable cancellation marker",
    )
    args = parser.parse_args(argv)
    if not args.confirm_cancel:
        parser.error("refusing to write a cancellation marker without --confirm-cancel")
    if not args.redis_url.strip():
        parser.error("Redis URL is required as --redis-url or AGENT_REDIS_URL")
    changed = cancel_run(
        args.redis_url,
        args.idempotency_key,
        retention_seconds=args.retention_seconds,
    )
    print(json.dumps({"status": "cancelled" if changed else "no_op"}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
