"""Run the documented scheduled briefing flow with a deterministic sink."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

from packages.agent_runtime import RunStatus
from packages.scheduler import Scheduler, ScheduleStatus, load_schedule
from services.bootstrap import build_runtime, demo_identity


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


class RecordingNotificationSink:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, str]] = []

    def send(self, *, channel, schedule, run) -> None:  # type: ignore[no-untyped-def]
        self.events.append((channel, schedule.id, run.status.value))


def main() -> int:
    definition = load_schedule("schedules/order-status-demo.yaml")
    _require(definition.timezone_name == "Asia/Taipei", "schedule timezone is not Asia/Taipei")
    _require(definition.permissions_mode == "read_only", "schedule is not read-only")
    _require(definition.notify_channel == "web", "web notification channel is not configured")
    sink = RecordingNotificationSink()
    scheduler = Scheduler(notifier=sink)
    scheduler.register(definition)
    trigger_time = datetime(2030, 1, 1, 2, 0, tzinfo=UTC)
    runtime, _audit = build_runtime()
    execution_count = 0

    def execute_agent(_definition, _key):  # type: ignore[no-untyped-def]
        nonlocal execution_count
        execution_count += 1
        result = runtime.run(
            "Provide the order status for the scheduled briefing.",
            demo_identity(),
            order_id="SO-1001",
        )
        _require(result.status is RunStatus.SUCCEEDED, "scheduled Agent execution failed")
        return result.message

    finding = scheduler.trigger(
        definition.id,
        trigger_time,
        execute_agent,
        finding=True,
    )
    duplicate = scheduler.trigger(
        definition.id,
        trigger_time,
        execute_agent,
        finding=True,
    )
    _require(finding.status is ScheduleStatus.SUCCEEDED, "finding run did not succeed")
    _require(finding.notification_sent, "finding run did not notify")
    _require(finding == duplicate, "duplicate delivery created a second effective run")
    _require(len(sink.events) == 1, "duplicate delivery sent a duplicate notification")
    _require(execution_count == 1, "duplicate delivery executed the Agent twice")

    quiet = replace(definition, id="quiet-order-status-demo", version="0.1.1")
    scheduler.register(quiet)
    quiet_run = scheduler.trigger(
        quiet.id,
        trigger_time,
        lambda _definition, _key: "no finding",
        finding=False,
    )
    _require(not quiet_run.notification_sent, "quiet success incorrectly notified")
    _require(len(sink.events) == 1, "quiet success changed notification count")

    cancelled = replace(definition, id="cancelled-order-status-demo", version="0.1.2")
    scheduler.register(cancelled)
    cancelled_key = "cancelled-order-status-demo:2030-01-01T01:00:00+00:00"
    scheduler.cancel(cancelled_key)
    cancelled_run = scheduler.trigger(
        cancelled.id,
        trigger_time,
        lambda _definition, _key: "must not execute",
    )
    _require(cancelled_run.status is ScheduleStatus.CANCELLED, "cancel signal was ignored")

    print(
        "scheduler-e2e-smoke: PASS "
        f"run={finding.idempotency_key} notification_events={len(sink.events)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
