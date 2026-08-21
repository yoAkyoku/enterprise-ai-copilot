"""Safe one-shot, interval and five-field cron scheduling.

This module is deliberately a small preview scheduler. It provides the
contracts, timezone handling, idempotency and pause/concurrency semantics that
the worker needs; distributed locking and durable queue delivery remain a
deployment adapter concern.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml


class ScheduleStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    PAUSED = "paused"
    EXPIRED = "expired"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


class NotificationSink(Protocol):
    """Explicitly injected destination for schedule notifications."""

    def send(
        self,
        *,
        channel: str,
        schedule: ScheduleDefinition,
        run: ScheduleRun,
    ) -> None:
        """Deliver one already-authorized notification."""


@dataclass(frozen=True)
class ScheduleDefinition:
    id: str
    version: str
    agent: str
    schedule_type: str
    timezone_name: str = "UTC"
    expression: str | None = None
    at: str | None = None
    interval_seconds: int | None = None
    max_runtime_seconds: int = 300
    max_concurrency: int = 1
    retry_limit: int = 0
    catch_up: bool = False
    permissions_mode: str = "read_only"
    query: str | None = None
    order_id: str | None = None
    expires_at: str | None = None
    notify_channel: str | None = None
    notify_only_if: str = "finding_or_failure"

    def timezone(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown schedule timezone: {self.timezone_name}") from exc

    def scheduled_for(self, now: datetime) -> datetime | None:
        local_now = now.astimezone(self.timezone())
        if self.schedule_type == "one_shot":
            if not self.at:
                raise ValueError("one_shot schedule requires at")
            target = datetime.fromisoformat(self.at)
            if target.tzinfo is None:
                target = target.replace(tzinfo=self.timezone())
            return target.astimezone(UTC) if target <= local_now else None
        if self.schedule_type == "interval":
            if not self.interval_seconds:
                raise ValueError("interval schedule requires interval_seconds")
            epoch = datetime(1970, 1, 1, tzinfo=UTC)
            current = now.astimezone(UTC)
            elapsed = int((current - epoch).total_seconds())
            slot = elapsed - (elapsed % self.interval_seconds)
            return epoch + timedelta(seconds=slot)
        if self.schedule_type == "cron":
            if not self.expression:
                raise ValueError("cron schedule requires expression")
            cursor = local_now.replace(second=0, microsecond=0)
            if cursor < local_now:
                cursor += timedelta(minutes=1)
            for _ in range(60 * 24 * 366):
                if CronExpression(self.expression).matches(cursor):
                    return cursor.astimezone(UTC)
                cursor += timedelta(minutes=1)
            raise ValueError("cron expression has no occurrence within one year")
        raise ValueError(f"unsupported schedule type: {self.schedule_type}")


@dataclass(frozen=True)
class ScheduleRun:
    schedule_id: str
    idempotency_key: str
    scheduled_for: str
    started_at: str
    finished_at: str
    status: ScheduleStatus
    attempts: int
    message: str
    notification_sent: bool = False
    notification_error: str | None = None


class CronExpression:
    """Small five-field cron matcher supporting lists, ranges and steps."""

    _LIMITS = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 6))

    def __init__(self, expression: str) -> None:
        fields = expression.split()
        if len(fields) != 5:
            raise ValueError("cron expression must contain five fields")
        self._values = tuple(
            self._parse_field(field, limits) for field, limits in zip(fields, self._LIMITS)
        )

    def matches(self, value: datetime) -> bool:
        candidates = (value.minute, value.hour, value.day, value.month, value.weekday())
        return all(candidate in allowed for candidate, allowed in zip(candidates, self._values))

    @staticmethod
    def _parse_field(field: str, limits: tuple[int, int]) -> frozenset[int]:
        minimum, maximum = limits
        result: set[int] = set()
        for item in field.split(","):
            if not item or item.count("/") > 1:
                raise ValueError(f"invalid cron field: {field}")
            base, _, step_text = item.partition("/")
            step = int(step_text) if step_text else 1
            if step <= 0:
                raise ValueError("cron step must be positive")
            if base == "*":
                start, end = minimum, maximum
            elif "-" in base:
                start_text, end_text = base.split("-", 1)
                start, end = int(start_text), int(end_text)
            else:
                start = end = int(base)
            if start < minimum or end > maximum or start > end:
                raise ValueError(f"cron value is outside {minimum}-{maximum}: {field}")
            result.update(range(start, end + 1, step))
        if not result:
            raise ValueError("cron field cannot be empty")
        return frozenset(result)


def load_schedule(path: str | Path) -> ScheduleDefinition:
    schedule_path = Path(path)
    raw = yaml.safe_load(schedule_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise TypeError("schedule must be a mapping")
    schedule = raw.get("schedule") or {}
    run = raw.get("run") or {}
    permissions = raw.get("permissions") or {}
    notify = raw.get("notify") or {}
    if (
        not isinstance(schedule, dict)
        or not isinstance(run, dict)
        or not isinstance(permissions, dict)
        or not isinstance(notify, dict)
    ):
        raise TypeError("schedule, run, permissions and notify must be mappings")
    query = run.get("query")
    order_id = run.get("order_id")
    if query is not None and not isinstance(query, str):
        raise TypeError("run.query must be a string")
    if order_id is not None and not isinstance(order_id, str):
        raise TypeError("run.order_id must be a string")
    return ScheduleDefinition(
        id=str(raw["id"]),
        version=str(raw["version"]),
        agent=str(raw["agent"]),
        schedule_type=str(schedule["type"]),
        timezone_name=str(schedule.get("timezone", "UTC")),
        expression=schedule.get("expression"),
        at=schedule.get("at"),
        interval_seconds=schedule.get("seconds"),
        max_runtime_seconds=int(run.get("max_runtime_seconds", 300)),
        max_concurrency=int(run.get("max_concurrency", 1)),
        retry_limit=int(run.get("retry_limit", 0)),
        catch_up=bool(run.get("catch_up", False)),
        permissions_mode=str(permissions.get("mode", "read_only")),
        query=query,
        order_id=order_id,
        expires_at=schedule.get("expires_at"),
        notify_channel=str(notify["channel"]) if notify.get("channel") is not None else None,
        notify_only_if=str(notify.get("only_if", "finding_or_failure")),
    )


class Scheduler:
    """Synchronous scheduler facade with idempotent run history."""

    def __init__(self, notifier: NotificationSink | None = None) -> None:
        self._definitions: dict[str, ScheduleDefinition] = {}
        self._history: dict[str, ScheduleRun] = {}
        self._paused: set[str] = set()
        self._active: dict[str, int] = {}
        self._cancelled: set[str] = set()
        self._notifier = notifier

    def register(self, definition: ScheduleDefinition) -> None:
        if (
            definition.id in self._definitions
            and self._definitions[definition.id].version != definition.version
        ):
            raise ValueError("schedule replacement requires an explicit version migration")
        if definition.max_concurrency <= 0 or definition.retry_limit < 0:
            raise ValueError("invalid schedule concurrency or retry limit")
        if definition.notify_only_if not in {"always", "finding_or_failure", "failure"}:
            raise ValueError("invalid schedule notification condition")
        if definition.notify_channel is not None and (
            not definition.notify_channel.strip() or len(definition.notify_channel) > 64
        ):
            raise ValueError("invalid schedule notification channel")
        definition.timezone()
        if definition.schedule_type == "cron" and definition.expression:
            CronExpression(definition.expression)
        self._definitions[definition.id] = definition

    def pause(self, schedule_id: str) -> None:
        self._require(schedule_id)
        self._paused.add(schedule_id)

    def resume(self, schedule_id: str) -> None:
        self._require(schedule_id)
        self._paused.discard(schedule_id)

    def cancel(self, idempotency_key: str) -> None:
        """Signal a queued or retrying run to stop before its next tool call."""

        if not idempotency_key or len(idempotency_key) > 256:
            raise ValueError("invalid schedule idempotency key")
        self._cancelled.add(idempotency_key)

    def history(self, schedule_id: str | None = None) -> list[ScheduleRun]:
        values = list(self._history.values())
        if schedule_id is not None:
            values = [item for item in values if item.schedule_id == schedule_id]
        return sorted(values, key=lambda item: item.started_at)

    def trigger(
        self,
        schedule_id: str,
        now: datetime,
        executor: Callable[[ScheduleDefinition, str], str],
        *,
        approval_granted: bool = False,
        finding: bool = False,
    ) -> ScheduleRun:
        definition = self._require(schedule_id)
        started = datetime.now(UTC)
        if schedule_id in self._paused:
            return self._record(
                schedule_id, "paused", started, started, 0, "schedule is paused", now
            )
        if definition.permissions_mode != "read_only" and not approval_granted:
            return self._record(
                schedule_id,
                "blocked",
                started,
                started,
                0,
                "scheduled write requires explicit approval",
                now,
            )
        if definition.expires_at:
            expires = datetime.fromisoformat(definition.expires_at)
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=definition.timezone())
            if now.astimezone(UTC) >= expires.astimezone(UTC):
                return self._record(
                    schedule_id, "expired", started, started, 0, "schedule has expired", now
                )
        scheduled = definition.scheduled_for(now)
        if scheduled is None:
            return self._record(schedule_id, "skipped", started, started, 0, "not due", now)
        key = f"{schedule_id}:{scheduled.isoformat()}"
        if key in self._history:
            return self._history[key]
        if key in self._cancelled:
            return self._record(
                schedule_id,
                "cancelled",
                started,
                started,
                0,
                "schedule was cancelled",
                scheduled,
            )
        if self._active.get(schedule_id, 0) >= definition.max_concurrency:
            return self._record(
                schedule_id, "skipped", started, started, 0, "concurrency limit reached", scheduled
            )
        self._active[schedule_id] = self._active.get(schedule_id, 0) + 1
        attempts = 0
        try:
            while attempts <= definition.retry_limit:
                if key in self._cancelled:
                    return self._record(
                        schedule_id,
                        "cancelled",
                        started,
                        datetime.now(UTC),
                        attempts,
                        "schedule was cancelled",
                        scheduled,
                    )
                attempts += 1
                try:
                    message = executor(definition, key)
                    finished = datetime.now(UTC)
                    run = self._record(
                        schedule_id, "succeeded", started, finished, attempts, message, scheduled
                    )
                    return self._notify(definition, run, finding=finding)
                except Exception:  # noqa: BLE001 - scheduler records controlled failure
                    if attempts > definition.retry_limit:
                        finished = datetime.now(UTC)
                        run = self._record(
                            schedule_id,
                            "failed",
                            started,
                            finished,
                            attempts,
                            "scheduled execution failed after retry limit",
                            scheduled,
                        )
                        return self._notify(definition, run, finding=False)
        finally:
            self._active[schedule_id] -= 1
            if self._active[schedule_id] == 0:
                del self._active[schedule_id]
        raise RuntimeError("scheduler reached an invalid terminal state")

    def _record(
        self,
        schedule_id: str,
        status: str,
        started: datetime,
        finished: datetime,
        attempts: int,
        message: str,
        scheduled: datetime,
    ) -> ScheduleRun:
        key = f"{schedule_id}:{scheduled.astimezone(UTC).isoformat()}"
        run = ScheduleRun(
            schedule_id=schedule_id,
            idempotency_key=key,
            scheduled_for=scheduled.astimezone(UTC).isoformat(),
            started_at=started.isoformat(),
            finished_at=finished.isoformat(),
            status=ScheduleStatus(status),
            attempts=attempts,
            message=message,
        )
        self._history[key] = run
        return run

    def _notify(
        self,
        definition: ScheduleDefinition,
        run: ScheduleRun,
        *,
        finding: bool,
    ) -> ScheduleRun:
        channel = definition.notify_channel
        if self._notifier is None or channel is None:
            return run
        should_notify = (
            definition.notify_only_if == "always"
            or (
                definition.notify_only_if == "finding_or_failure"
                and (finding or run.status in {ScheduleStatus.FAILED, ScheduleStatus.BLOCKED})
            )
            or (
                definition.notify_only_if == "failure"
                and run.status in {ScheduleStatus.FAILED, ScheduleStatus.BLOCKED}
            )
        )
        if not should_notify:
            return run
        try:
            self._notifier.send(channel=channel, schedule=definition, run=run)
        except Exception as exc:  # noqa: BLE001 - retain notification failure evidence
            updated = replace(run, notification_error=type(exc).__name__)
            self._history[run.idempotency_key] = updated
            return updated
        updated = replace(run, notification_sent=True)
        self._history[run.idempotency_key] = updated
        return updated

    def _require(self, schedule_id: str) -> ScheduleDefinition:
        try:
            return self._definitions[schedule_id]
        except KeyError as exc:
            raise KeyError(f"unknown schedule: {schedule_id}") from exc
