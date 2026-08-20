"""Durable queue adapters for scheduled Agent jobs."""

from __future__ import annotations

import json
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from typing import Protocol


class QueueError(RuntimeError):
    """Raised when a queue operation cannot safely complete."""


@dataclass(frozen=True)
class Job:
    id: str
    payload: dict[str, object]
    enqueued_at: str
    receipt: str | None = None


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
        receipt = str(message[0])
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
