"""Append-only audit adapters for in-memory and SQLite-backed runs."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import MutableSequence, Sequence
from pathlib import Path
from typing import Protocol

from .models import AuditEvent


class EventStore(Protocol):
    def append(self, event: AuditEvent) -> None:
        """Persist one event without replacing an existing event."""

    def list_events(
        self,
        *,
        trace_id: str | None = None,
        run_id: str | None = None,
        workspace_id: str | None = None,
    ) -> list[AuditEvent]:
        """Return events ordered by insertion."""

    def healthcheck(self) -> bool:
        """Return whether the backing store can accept a probe query."""


class SqliteAuditStore:
    """Small durable event store for local preview and single-worker deployments.

    SQLite is deliberately an adapter, not a claim of clustered durability.
    Production deployments must use a transactional shared store and retain
    the same append-only event contract.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS run_events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                request_id TEXT NOT NULL,
                trace_id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                workspace_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        self._connection.commit()

    def append(self, event: AuditEvent) -> None:
        self._connection.execute(
            """
            INSERT INTO run_events
              (event_type, request_id, trace_id, run_id, workspace_id, agent_id, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.event_type,
                event.request_id,
                event.trace_id,
                event.run_id,
                event.workspace_id,
                event.agent_id,
                json.dumps(event.payload, sort_keys=True, separators=(",", ":")),
                event.created_at,
            ),
        )
        self._connection.commit()

    def list_events(
        self,
        *,
        trace_id: str | None = None,
        run_id: str | None = None,
        workspace_id: str | None = None,
    ) -> list[AuditEvent]:
        clauses: list[str] = []
        values: list[str] = []
        if trace_id:
            clauses.append("trace_id = ?")
            values.append(trace_id)
        if run_id:
            clauses.append("run_id = ?")
            values.append(run_id)
        if workspace_id:
            clauses.append("workspace_id = ?")
            values.append(workspace_id)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._connection.execute(
            "SELECT event_type, request_id, trace_id, run_id, workspace_id, agent_id, payload_json, created_at "
            f"FROM run_events{where} ORDER BY sequence",
            values,
        ).fetchall()
        return [
            AuditEvent(
                event_type=row[0],
                request_id=row[1],
                trace_id=row[2],
                run_id=row[3],
                workspace_id=row[4],
                agent_id=row[5],
                payload=json.loads(row[6]),
                created_at=row[7],
            )
            for row in rows
        ]

    def close(self) -> None:
        self._connection.close()


class AuditLog:
    """Append-only audit log with an optional durable event sink."""

    def __init__(
        self,
        events: MutableSequence[AuditEvent] | None = None,
        store: EventStore | None = None,
    ) -> None:
        self.events: MutableSequence[AuditEvent] = events if events is not None else []
        self._store = store

    def append(self, event: AuditEvent) -> None:
        self.events.append(event)
        if self._store is not None:
            self._store.append(event)

    def healthcheck(self) -> bool:
        """Probe the durable event store when one is configured."""

        if self._store is None:
            return True
        healthcheck = getattr(self._store, "healthcheck", None)
        if healthcheck is None:
            return True
        try:
            return bool(healthcheck())
        except Exception:  # noqa: BLE001 - readiness must fail closed
            return False

    def list_events(
        self,
        *,
        trace_id: str | None = None,
        run_id: str | None = None,
        workspace_id: str | None = None,
    ) -> Sequence[AuditEvent]:
        if self._store is not None:
            return self._store.list_events(
                trace_id=trace_id, run_id=run_id, workspace_id=workspace_id
            )
        return [
            event
            for event in self.events
            if (trace_id is None or event.trace_id == trace_id)
            and (run_id is None or event.run_id == run_id)
            and (workspace_id is None or event.workspace_id == workspace_id)
        ]
