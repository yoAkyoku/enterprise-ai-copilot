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
        tenant_id: str | None = None,
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
                tenant_id TEXT,
                agent_id TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        self._ensure_tenant_column()
        self._connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_run_events_scope "
            "ON run_events(workspace_id, tenant_id, sequence)"
        )
        self._connection.commit()

    def _ensure_tenant_column(self) -> None:
        columns = {str(row[1]) for row in self._connection.execute("PRAGMA table_info(run_events)")}
        if "tenant_id" not in columns:
            self._connection.execute("ALTER TABLE run_events ADD COLUMN tenant_id TEXT")
        rows = self._connection.execute(
            "SELECT sequence, payload_json FROM run_events WHERE tenant_id IS NULL"
        ).fetchall()
        for sequence, payload_json in rows:
            try:
                payload = json.loads(str(payload_json))
            except (TypeError, ValueError):
                continue
            tenant_id = payload.get("tenant_id") if isinstance(payload, dict) else None
            if isinstance(tenant_id, str) and tenant_id.strip():
                self._connection.execute(
                    "UPDATE run_events SET tenant_id = ? WHERE sequence = ?",
                    (tenant_id, sequence),
                )

    def append(self, event: AuditEvent) -> None:
        self._connection.execute(
            """
            INSERT INTO run_events
              (event_type, request_id, trace_id, run_id, workspace_id, tenant_id, agent_id, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.event_type,
                event.request_id,
                event.trace_id,
                event.run_id,
                event.workspace_id,
                event.tenant_id,
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
        tenant_id: str | None = None,
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
        if tenant_id:
            clauses.append("tenant_id = ?")
            values.append(tenant_id)
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
        tenant_id: str | None = None,
    ) -> Sequence[AuditEvent]:
        """Return events filtered to the requested workspace and tenant.

        Older storage schemas keep tenant scope inside the event payload. The
        facade applies the tenant filter after the durable query so legacy
        SQLite/PostgreSQL rows cannot broaden a tenant-scoped API response.
        Events without an explicit tenant claim are intentionally excluded from
        tenant-scoped reads.
        """

        if self._store is not None:
            events = self._store.list_events(
                trace_id=trace_id,
                run_id=run_id,
                workspace_id=workspace_id,
                tenant_id=tenant_id,
            )
        else:
            events = [
                event
                for event in self.events
                if (trace_id is None or event.trace_id == trace_id)
                and (run_id is None or event.run_id == run_id)
                and (workspace_id is None or event.workspace_id == workspace_id)
            ]
        if tenant_id is None:
            return events
        return [event for event in events if event.payload.get("tenant_id") == tenant_id]
