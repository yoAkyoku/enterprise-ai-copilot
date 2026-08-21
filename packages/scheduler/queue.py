"""Durable queue adapters for scheduled Agent jobs."""

from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from typing import Literal, Protocol


class QueueError(RuntimeError):
    """Raised when a queue operation cannot safely complete."""


@dataclass(frozen=True)
class Job:
    id: str
    payload: dict[str, object]
    enqueued_at: str
    receipt: str | None = None


@dataclass(frozen=True)
class RunClaim:
    """A distributed execution claim for one schedule idempotency key."""

    status: Literal["claimed", "in_progress", "completed"]
    token: str | None = None


class JobQueue(Protocol):
    def enqueue(self, payload: dict[str, object]) -> Job:
        """Enqueue one JSON-safe job."""

    def receive(self, *, block_seconds: int = 5) -> Job | None:
        """Receive a new or abandoned job."""

    def ack(self, job: Job) -> None:
        """Acknowledge successful terminal processing."""


class InMemoryJobQueue:
    """Deterministic queue used by tests and development preview."""

    def __init__(self) -> None:
        self._jobs: deque[Job] = deque()
        self._lock = threading.Lock()

    def enqueue(self, payload: dict[str, object]) -> Job:
        job = Job(id=uuid.uuid4().hex, payload=dict(payload), enqueued_at=str(time.time()))
        with self._lock:
            self._jobs.append(job)
        return job

    def receive(self, *, block_seconds: int = 5) -> Job | None:
        del block_seconds
        with self._lock:
            return self._jobs.popleft() if self._jobs else None

    def ack(self, job: Job) -> None:
        del job


class RedisJobQueue:
    """Redis Streams queue with consumer groups and abandoned-job claiming."""

    def __init__(
        self,
        client: object,
        *,
        stream: str = "agent:jobs",
        group: str = "agent-workers",
        consumer: str | None = None,
        claim_after_seconds: int = 60,
    ) -> None:
        if not stream or not group or len(stream) > 200 or len(group) > 200:
            raise ValueError("Redis stream and group names are invalid")
        if claim_after_seconds <= 0 or claim_after_seconds > 86400:
            raise ValueError("claim timeout is invalid")
        self.client = client
        self.stream = stream
        self.group = group
        self.consumer = consumer or f"worker-{uuid.uuid4().hex}"
        self.claim_after_seconds = claim_after_seconds
        self._group_lock = threading.Lock()
        self._group_ready = False

    def _ensure_group(self) -> None:
        if self._group_ready:
            return
        with self._group_lock:
            if self._group_ready:
                return
            try:
                self.client.xgroup_create(self.stream, self.group, id="0-0", mkstream=True)  # type: ignore[attr-defined]
            except Exception as exc:
                if "BUSYGROUP" not in str(exc):
                    raise QueueError("Redis consumer group could not be initialized") from exc
            self._group_ready = True

    def enqueue(self, payload: dict[str, object]) -> Job:
        try:
            encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            raise QueueError("job payload must be JSON serializable") from exc
        if len(encoded.encode("utf-8")) > 100_000:
            raise QueueError("job payload is too large")
        self._ensure_group()
        job_id = uuid.uuid4().hex
        try:
            receipt = self.client.xadd(  # type: ignore[attr-defined]
                self.stream,
                {"job_id": job_id, "payload": encoded, "enqueued_at": str(time.time())},
            )
        except Exception as exc:
            raise QueueError("Redis job enqueue failed") from exc
        return Job(
            id=job_id, payload=dict(payload), enqueued_at=str(time.time()), receipt=str(receipt)
        )

    def enqueue_once(
        self,
        payload: dict[str, object],
        idempotency_key: str,
        *,
        retention_seconds: int = 30 * 86400,
    ) -> Job | None:
        """Enqueue one schedule slot without duplicating it after producer restart."""

        if not idempotency_key or len(idempotency_key) > 256:
            raise ValueError("schedule enqueue idempotency key is invalid")
        if retention_seconds <= 0 or retention_seconds > 365 * 86400:
            raise ValueError("schedule enqueue retention is invalid")
        state_key = self._enqueue_state_key(idempotency_key)
        try:
            created = self.client.set(  # type: ignore[attr-defined]
                state_key, "enqueued", nx=True, ex=retention_seconds
            )
            if not created:
                return None
            try:
                return self.enqueue(payload)
            except Exception:
                self.client.delete(state_key)  # type: ignore[attr-defined]
                raise
        except Exception as exc:
            if isinstance(exc, QueueError):
                raise
            raise QueueError("Redis schedule enqueue claim failed") from exc

    def receive(self, *, block_seconds: int = 5) -> Job | None:
        if block_seconds < 0 or block_seconds > 300:
            raise ValueError("block timeout is invalid")
        self._ensure_group()
        messages = self._claim_abandoned()
        if not messages:
            try:
                batches = self.client.xreadgroup(  # type: ignore[attr-defined]
                    self.group,
                    self.consumer,
                    {self.stream: ">"},
                    count=1,
                    block=block_seconds * 1000,
                )
            except Exception as exc:
                raise QueueError("Redis job receive failed") from exc
            messages = self._flatten(batches)
        return self._decode(messages[0]) if messages else None

    def ack(self, job: Job) -> None:
        if not job.receipt:
            return
        try:
            self.client.xack(self.stream, self.group, job.receipt)  # type: ignore[attr-defined]
        except Exception as exc:
            raise QueueError("Redis job acknowledgement failed") from exc

    def claim_run(
        self,
        idempotency_key: str,
        *,
        lease_seconds: int = 300,
        retention_seconds: int = 86400,
    ) -> RunClaim:
        """Claim one schedule run, retaining terminal completion for dedupe."""

        if not idempotency_key or len(idempotency_key) > 256:
            raise ValueError("run idempotency key is invalid")
        if lease_seconds <= 0 or lease_seconds > 86400:
            raise ValueError("run lease is invalid")
        if retention_seconds <= 0 or retention_seconds > 7 * 86400:
            raise ValueError("run completion retention is invalid")
        state_key = self._run_state_key(idempotency_key)
        token = uuid.uuid4().hex
        running_value = f"running:{token}"
        try:
            current = self.client.get(state_key)  # type: ignore[attr-defined]
            if current is None:
                created = self.client.set(  # type: ignore[attr-defined]
                    state_key, running_value, nx=True, ex=lease_seconds
                )
                if created:
                    return RunClaim("claimed", token)
                current = self.client.get(state_key)  # type: ignore[attr-defined]
            normalized = current.decode() if isinstance(current, bytes) else str(current)
            if normalized == "completed":
                return RunClaim("completed")
            return RunClaim("in_progress")
        except Exception as exc:
            raise QueueError("Redis run claim failed") from exc

    def complete_run(
        self,
        idempotency_key: str,
        token: str,
        *,
        retention_seconds: int = 86400,
    ) -> None:
        """Atomically convert an owned lease into a terminal dedupe marker."""

        if not idempotency_key or len(idempotency_key) > 256:
            raise ValueError("run idempotency key is invalid")
        if not token or len(token) > 128:
            raise ValueError("run claim token is invalid")
        if retention_seconds <= 0 or retention_seconds > 7 * 86400:
            raise ValueError("run completion retention is invalid")
        state_key = self._run_state_key(idempotency_key)
        script = (
            "if redis.call('get', KEYS[1]) == ARGV[1] then "
            "redis.call('set', KEYS[1], 'completed', 'EX', ARGV[2]); return 1; "
            "end; return 0"
        )
        try:
            result = self.client.eval(  # type: ignore[attr-defined]
                script, 1, state_key, f"running:{token}", retention_seconds
            )
        except Exception as exc:
            raise QueueError("Redis run completion failed") from exc
        if result != 1:
            raise QueueError("Redis run claim was lost before completion")

    def _run_state_key(self, idempotency_key: str) -> str:
        digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
        return f"{self.stream}:runs:{digest}"

    def _enqueue_state_key(self, idempotency_key: str) -> str:
        digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
        return f"{self.stream}:enqueued:{digest}"

    def _claim_abandoned(self) -> list[tuple[object, object]]:
        try:
            result = self.client.xautoclaim(  # type: ignore[attr-defined]
                self.stream,
                self.group,
                self.consumer,
                self.claim_after_seconds * 1000,
                "0-0",
                count=1,
            )
        except Exception as exc:
            raise QueueError("Redis abandoned-job claim failed") from exc
        return list(result[1]) if isinstance(result, (list, tuple)) and len(result) > 1 else []

    @staticmethod
    def _flatten(batches: object) -> list[tuple[object, object]]:
        if not isinstance(batches, list):
            return []
        messages: list[tuple[object, object]] = []
        for batch in batches:
            if isinstance(batch, (list, tuple)) and len(batch) > 1 and isinstance(batch[1], list):
                messages.extend(
                    item for item in batch[1] if isinstance(item, (list, tuple)) and len(item) == 2
                )
        return messages

    @staticmethod
    def _decode(message: tuple[object, object]) -> Job:
        raw_receipt = message[0]
        receipt = raw_receipt.decode() if isinstance(raw_receipt, bytes) else str(raw_receipt)
        fields = message[1]
        if not isinstance(fields, dict):
            raise QueueError("Redis job fields are invalid")
        try:
            raw_payload = fields.get(b"payload", fields.get("payload"))
            payload = json.loads(
                raw_payload.decode() if isinstance(raw_payload, bytes) else str(raw_payload)
            )
            job_id = fields.get(b"job_id", fields.get("job_id"))
            enqueued_at = fields.get(b"enqueued_at", fields.get("enqueued_at"))
        except (AttributeError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise QueueError("Redis job payload is invalid") from exc
        if not isinstance(payload, dict) or job_id is None or enqueued_at is None:
            raise QueueError("Redis job fields are incomplete")
        return Job(
            id=job_id.decode() if isinstance(job_id, bytes) else str(job_id),
            payload=payload,
            enqueued_at=enqueued_at.decode()
            if isinstance(enqueued_at, bytes)
            else str(enqueued_at),
            receipt=receipt,
        )
