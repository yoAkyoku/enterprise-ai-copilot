"""Durable run records for restart-safe API reads and idempotency lookup."""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .models import IdentityContext, RunResult, RunStatus


@dataclass(frozen=True)
class StoredRun:
    result: RunResult
    identity: IdentityContext
    idempotency_key: str | None = None


class RunStore(Protocol):
    def save(self, run: StoredRun) -> None:
        """Persist a run record without changing its result."""

    def get(self, run_id: str, identity: IdentityContext) -> StoredRun | None:
        """Return a run only when every identity scope matches."""

    def find_idempotent(self, identity: IdentityContext, idempotency_key: str) -> StoredRun | None:
        """Find a prior run created by the same identity and key."""


class SQLiteRunStore:
    """SQLite adapter for local and single-process durable API deployments."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(self.path, check_same_thread=False, timeout=10)
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_runs (
                run_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                trace_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                message TEXT NOT NULL,
                source_id TEXT,
                observed_at TEXT,
                external_ref TEXT,
                user_id TEXT NOT NULL,
                workspace_id TEXT NOT NULL,
                tenant_id TEXT NOT NULL,
                role TEXT NOT NULL,
                idempotency_key TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(workspace_id, tenant_id, user_id, role, idempotency_key)
            )
            """
        )
        self._connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_agent_runs_scope "
            "ON agent_runs(workspace_id, tenant_id, user_id, created_at)"
        )
        self._connection.commit()

    @staticmethod
    def _from_row(row: tuple[object, ...]) -> StoredRun:
        result = RunResult(
            status=RunStatus(str(row[1])),
            run_id=str(row[0]),
            trace_id=str(row[2]),
            agent_id=str(row[3]),
            message=str(row[4]),
            source_id=str(row[5]) if row[5] is not None else None,
            observed_at=str(row[6]) if row[6] is not None else None,
            external_ref=str(row[7]) if row[7] is not None else None,
        )
        identity = IdentityContext(
            user_id=str(row[8]),
            workspace_id=str(row[9]),
            tenant_id=str(row[10]),
            role=str(row[11]),
        )
        return StoredRun(
            result=result, identity=identity, idempotency_key=str(row[12]) if row[12] else None
        )

    @staticmethod
    def _select() -> str:
        return (
            "SELECT run_id, status, trace_id, agent_id, message, source_id, observed_at, external_ref, "
            "user_id, workspace_id, tenant_id, role, idempotency_key, created_at FROM agent_runs"
        )

    def save(self, run: StoredRun) -> None:
        with self._lock:
            self._connection.execute(
                "INSERT INTO agent_runs "
                "(run_id, status, trace_id, agent_id, message, source_id, observed_at, external_ref, "
                "user_id, workspace_id, tenant_id, role, idempotency_key, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))",
                (
                    run.result.run_id,
                    run.result.status.value,
                    run.result.trace_id,
                    run.result.agent_id,
                    run.result.message,
                    run.result.source_id,
                    run.result.observed_at,
                    run.result.external_ref,
                    run.identity.user_id,
                    run.identity.workspace_id,
                    run.identity.tenant_id,
                    run.identity.role,
                    run.idempotency_key,
                ),
            )
            self._connection.commit()

    def get(self, run_id: str, identity: IdentityContext) -> StoredRun | None:
        with self._lock:
            row = self._connection.execute(
                f"{self._select()} WHERE run_id = ? AND user_id = ? AND workspace_id = ? AND tenant_id = ? AND role = ?",
                (
                    run_id,
                    identity.user_id,
                    identity.workspace_id,
                    identity.tenant_id,
                    identity.role,
                ),
            ).fetchone()
        return self._from_row(row) if row else None

    def find_idempotent(self, identity: IdentityContext, idempotency_key: str) -> StoredRun | None:
        with self._lock:
            row = self._connection.execute(
                f"{self._select()} WHERE user_id = ? AND workspace_id = ? AND tenant_id = ? AND role = ? "
                "AND idempotency_key = ?",
                (
                    identity.user_id,
                    identity.workspace_id,
                    identity.tenant_id,
                    identity.role,
                    idempotency_key,
                ),
            ).fetchone()
        return self._from_row(row) if row else None

    def close(self) -> None:
        with self._lock:
            self._connection.close()
