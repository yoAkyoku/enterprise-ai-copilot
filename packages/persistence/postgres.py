"""PostgreSQL adapters for shared production state.

The adapters keep the same scoped contracts as the SQLite preview stores. SQL
is fixed platform code with bound parameters; no Agent, Skill or MCP tool gets
an SQL connection. Constructors create the checked-in tables idempotently so a
fresh container cannot serve requests before migrations have been applied, but
operators should still run ``scripts/migrate_postgres.py`` as a deployment
step to record the migration set.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from packages.agent_runtime.models import (
    AuditEvent,
    IdentityContext,
    RunResult,
    RunStatus,
    ToolRisk,
)
from packages.agent_runtime.approvals import ApprovalError, ApprovalRecord, ApprovalStatus
from packages.agent_runtime.runs import StoredRun
from packages.attachments.service import AttachmentRecord


def _connect(database_url: str) -> Any:
    if not database_url.strip() or len(database_url) > 4096:
        raise ValueError("PostgreSQL database URL is invalid")
    try:
        import psycopg
    except ImportError as exc:  # pragma: no cover - exercised by production install
        raise RuntimeError("psycopg is required when AGENT_STORAGE_MODE=postgres") from exc
    try:
        return psycopg.connect(database_url, connect_timeout=10)
    except Exception as exc:  # noqa: BLE001 - do not leak DSN details
        raise RuntimeError("PostgreSQL connection failed") from exc


class _PostgresStore:
    def __init__(self, database_url: str) -> None:
        self._lock = threading.RLock()
        self._connection = _connect(database_url)
        self._initialize()

    def _execute_script(self, statements: Sequence[str]) -> None:
        with self._connection.cursor() as cursor:
            for statement in statements:
                cursor.execute(statement)
        self._connection.commit()

    def healthcheck(self) -> bool:
        """Probe the existing connection without exposing database details."""

        with self._lock:
            try:
                with self._connection.cursor() as cursor:
                    cursor.execute("SELECT 1")
                    return cursor.fetchone() == (1,)
            except Exception:  # noqa: BLE001 - readiness must fail closed
                return False

    def close(self) -> None:
        with self._lock:
            self._connection.close()


class PostgresAuditStore(_PostgresStore):
    """Transactional append-only audit store for clustered API replicas."""

    def _initialize(self) -> None:
        self._execute_script(
            (
                """
                CREATE TABLE IF NOT EXISTS run_events (
                    sequence BIGSERIAL PRIMARY KEY,
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
                """,
                "ALTER TABLE run_events ADD COLUMN IF NOT EXISTS tenant_id TEXT",
                "UPDATE run_events SET tenant_id = payload_json::jsonb ->> 'tenant_id' WHERE tenant_id IS NULL",
                "CREATE INDEX IF NOT EXISTS idx_run_events_scope ON run_events(workspace_id, tenant_id, sequence)",
            )
        )

    def append(self, event: AuditEvent) -> None:
        with self._lock:
            try:
                with self._connection.cursor() as cursor:
                    cursor.execute(
                        "INSERT INTO run_events "
                        "(event_type, request_id, trace_id, run_id, workspace_id, tenant_id, agent_id, payload_json, created_at) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
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
            except Exception:
                self._connection.rollback()
                raise

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
            clauses.append("trace_id = %s")
            values.append(trace_id)
        if run_id:
            clauses.append("run_id = %s")
            values.append(run_id)
        if workspace_id:
            clauses.append("workspace_id = %s")
            values.append(workspace_id)
        if tenant_id:
            clauses.append("tenant_id = %s")
            values.append(tenant_id)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock, self._connection.cursor() as cursor:
            cursor.execute(
                "SELECT event_type, request_id, trace_id, run_id, workspace_id, agent_id, payload_json, created_at "
                f"FROM run_events{where} ORDER BY sequence",
                values,
            )
            rows = cursor.fetchall()
        return [
            AuditEvent(
                event_type=str(row[0]),
                request_id=str(row[1]),
                trace_id=str(row[2]),
                run_id=str(row[3]),
                workspace_id=str(row[4]),
                agent_id=str(row[5]),
                payload=json.loads(str(row[6])),
                created_at=str(row[7]),
            )
            for row in rows
        ]


class PostgresRunStore(_PostgresStore):
    """Scoped, idempotent run store for multiple API replicas."""

    def _initialize(self) -> None:
        self._execute_script(
            (
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
                """,
                "CREATE INDEX IF NOT EXISTS idx_agent_runs_scope ON agent_runs(workspace_id, tenant_id, user_id, created_at)",
            )
        )

    @staticmethod
    def _from_row(row: Sequence[object]) -> StoredRun:
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
        identity = IdentityContext(str(row[8]), str(row[9]), str(row[10]), str(row[11]))
        return StoredRun(
            result=result,
            identity=identity,
            idempotency_key=str(row[12]) if row[12] else None,
        )

    @staticmethod
    def _select() -> str:
        return (
            "SELECT run_id, status, trace_id, agent_id, message, source_id, observed_at, external_ref, "
            "user_id, workspace_id, tenant_id, role, idempotency_key, created_at FROM agent_runs"
        )

    def save(self, run: StoredRun) -> StoredRun:
        with self._lock:
            try:
                with self._connection.cursor() as cursor:
                    statement = (
                        "INSERT INTO agent_runs "
                        "(run_id, status, trace_id, agent_id, message, source_id, observed_at, external_ref, "
                        "user_id, workspace_id, tenant_id, role, idempotency_key, created_at) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW()::text)"
                    )
                    if run.idempotency_key:
                        statement += (
                            " ON CONFLICT (workspace_id, tenant_id, user_id, role, idempotency_key)"
                            " DO NOTHING"
                        )
                    cursor.execute(
                        statement,
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
            except Exception:
                self._connection.rollback()
                raise
        if run.idempotency_key:
            return self.find_idempotent(run.identity, run.idempotency_key) or run
        return run

    def _find(self, query: str, values: Sequence[object]) -> StoredRun | None:
        with self._lock, self._connection.cursor() as cursor:
            cursor.execute(query, values)
            row = cursor.fetchone()
        return self._from_row(row) if row else None

    def get(self, run_id: str, identity: IdentityContext) -> StoredRun | None:
        return self._find(
            f"{self._select()} WHERE run_id = %s AND user_id = %s AND workspace_id = %s AND tenant_id = %s AND role = %s",
            (run_id, identity.user_id, identity.workspace_id, identity.tenant_id, identity.role),
        )

    def find_idempotent(self, identity: IdentityContext, idempotency_key: str) -> StoredRun | None:
        return self._find(
            f"{self._select()} WHERE user_id = %s AND workspace_id = %s AND tenant_id = %s AND role = %s AND idempotency_key = %s",
            (
                identity.user_id,
                identity.workspace_id,
                identity.tenant_id,
                identity.role,
                idempotency_key,
            ),
        )


class PostgresApprovalStore(_PostgresStore):
    """Atomic, scope-filtered approval store for shared deployments."""

    def _initialize(self) -> None:
        self._execute_script(
            (
                """
                CREATE TABLE IF NOT EXISTS approval_requests (
                    id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    requester_user_id TEXT NOT NULL,
                    requester_role TEXT NOT NULL,
                    tool_name TEXT NOT NULL,
                    arguments_json TEXT NOT NULL,
                    arguments_hash TEXT NOT NULL,
                    risk TEXT NOT NULL,
                    idempotency_key TEXT,
                    requested_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    approver_user_id TEXT,
                    decided_at TEXT,
                    token_hash TEXT,
                    UNIQUE(workspace_id, tenant_id, requester_user_id, idempotency_key)
                )
                """,
                "CREATE INDEX IF NOT EXISTS idx_approval_scope ON approval_requests(workspace_id, tenant_id, status, requested_at)",
            )
        )

    @staticmethod
    def _from_row(row: Sequence[object]) -> ApprovalRecord:
        return ApprovalRecord(
            id=str(row[0]),
            requester=IdentityContext(str(row[3]), str(row[1]), str(row[2]), str(row[4])),
            tool_name=str(row[5]),
            arguments=json.loads(str(row[6])),
            arguments_hash=str(row[7]),
            risk=ToolRisk(str(row[8])),
            idempotency_key=str(row[9]) if row[9] is not None else None,
            requested_at=str(row[10]),
            expires_at=str(row[11]),
            status=str(row[12]),
            approver_user_id=str(row[13]) if row[13] is not None else None,
            decided_at=str(row[14]) if row[14] is not None else None,
        )

    @staticmethod
    def _select() -> str:
        return (
            "SELECT id, workspace_id, tenant_id, requester_user_id, requester_role, tool_name, arguments_json, "
            "arguments_hash, risk, idempotency_key, requested_at, expires_at, status, approver_user_id, decided_at, token_hash "
            "FROM approval_requests"
        )

    def create(self, record: ApprovalRecord) -> None:
        with self._lock:
            try:
                with self._connection.cursor() as cursor:
                    cursor.execute(
                        "INSERT INTO approval_requests "
                        "(id, workspace_id, tenant_id, requester_user_id, requester_role, tool_name, arguments_json, "
                        "arguments_hash, risk, idempotency_key, requested_at, expires_at, status) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                        (
                            record.id,
                            record.requester.workspace_id,
                            record.requester.tenant_id,
                            record.requester.user_id,
                            record.requester.role,
                            record.tool_name,
                            json.dumps(
                                record.arguments,
                                sort_keys=True,
                                separators=(",", ":"),
                                ensure_ascii=False,
                            ),
                            record.arguments_hash,
                            record.risk.value,
                            record.idempotency_key,
                            record.requested_at,
                            record.expires_at,
                            record.status,
                        ),
                    )
                self._connection.commit()
            except Exception as exc:
                self._connection.rollback()
                raise ApprovalError("approval id or idempotency key already exists") from exc

    def _find(self, query: str, values: Sequence[object]) -> ApprovalRecord | None:
        with self._lock, self._connection.cursor() as cursor:
            cursor.execute(query, values)
            row = cursor.fetchone()
        return self._from_row(row) if row else None

    def get(self, approval_id: str, *, workspace_id: str, tenant_id: str) -> ApprovalRecord | None:
        return self._find(
            f"{self._select()} WHERE id = %s AND workspace_id = %s AND tenant_id = %s",
            (approval_id, workspace_id, tenant_id),
        )

    def list(
        self, *, workspace_id: str, tenant_id: str, status: str | None = None
    ) -> Sequence[ApprovalRecord]:
        clauses = ["workspace_id = %s", "tenant_id = %s"]
        values: list[object] = [workspace_id, tenant_id]
        if status is not None:
            clauses.append("status = %s")
            values.append(status)
        with self._lock, self._connection.cursor() as cursor:
            cursor.execute(
                f"{self._select()} WHERE {' AND '.join(clauses)} ORDER BY requested_at DESC",
                values,
            )
            rows = cursor.fetchall()
        return tuple(self._from_row(row) for row in rows)

    def find_idempotent(
        self, *, workspace_id: str, tenant_id: str, user_id: str, idempotency_key: str
    ) -> ApprovalRecord | None:
        return self._find(
            f"{self._select()} WHERE workspace_id = %s AND tenant_id = %s AND requester_user_id = %s AND idempotency_key = %s",
            (workspace_id, tenant_id, user_id, idempotency_key),
        )

    def decide(
        self,
        approval_id: str,
        *,
        workspace_id: str,
        tenant_id: str,
        status: str,
        approver_user_id: str | None,
        decided_at: str,
        token_hash: str | None,
    ) -> ApprovalRecord | None:
        with self._lock:
            try:
                with self._connection.cursor() as cursor:
                    cursor.execute(
                        "UPDATE approval_requests SET status = %s, approver_user_id = %s, decided_at = %s, token_hash = %s "
                        "WHERE id = %s AND workspace_id = %s AND tenant_id = %s AND status = %s",
                        (
                            status,
                            approver_user_id,
                            decided_at,
                            token_hash,
                            approval_id,
                            workspace_id,
                            tenant_id,
                            ApprovalStatus.PENDING,
                        ),
                    )
                    changed = cursor.rowcount
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise
        if changed != 1:
            return None
        return self.get(approval_id, workspace_id=workspace_id, tenant_id=tenant_id)

    def consume(
        self,
        approval_id: str,
        *,
        workspace_id: str,
        tenant_id: str,
        token_hash: str,
    ) -> bool:
        with self._lock:
            try:
                with self._connection.cursor() as cursor:
                    cursor.execute(
                        "UPDATE approval_requests SET status = %s, token_hash = NULL "
                        "WHERE id = %s AND workspace_id = %s AND tenant_id = %s "
                        "AND status = %s AND token_hash = %s",
                        (
                            ApprovalStatus.CONSUMED,
                            approval_id,
                            workspace_id,
                            tenant_id,
                            ApprovalStatus.APPROVED,
                            token_hash,
                        ),
                    )
                    changed = cursor.rowcount
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise
        return changed == 1

    def token_hash(self, approval_id: str, *, workspace_id: str, tenant_id: str) -> str | None:
        with self._lock, self._connection.cursor() as cursor:
            cursor.execute(
                "SELECT token_hash FROM approval_requests WHERE id = %s AND workspace_id = %s AND tenant_id = %s",
                (approval_id, workspace_id, tenant_id),
            )
            row = cursor.fetchone()
        return str(row[0]) if row and row[0] else None


class PostgresAttachmentStore(_PostgresStore):
    """Tenant/workspace/user-scoped attachment metadata for shared deployments."""

    def _initialize(self) -> None:
        self._execute_script(
            (
                """
                CREATE TABLE IF NOT EXISTS attachments (
                    id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    content_type TEXT NOT NULL,
                    image_format TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    sha256 TEXT NOT NULL,
                    width INTEGER NOT NULL,
                    height INTEGER NOT NULL,
                    storage_path TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                )
                """,
                "CREATE INDEX IF NOT EXISTS idx_attachments_scope ON attachments(workspace_id, tenant_id, user_id, created_at)",
            )
        )

    @staticmethod
    def _from_row(row: Sequence[object]) -> AttachmentRecord:
        return AttachmentRecord(
            id=str(row[0]),
            workspace_id=str(row[1]),
            tenant_id=str(row[2]),
            user_id=str(row[3]),
            filename=str(row[4]),
            content_type=str(row[5]),
            image_format=str(row[6]),
            size_bytes=int(row[7]),
            sha256=str(row[8]),
            width=int(row[9]),
            height=int(row[10]),
            storage_path=str(row[11]),
            created_at=str(row[12]),
        )

    @staticmethod
    def _select() -> str:
        return (
            "SELECT id, workspace_id, tenant_id, user_id, filename, content_type, image_format, "
            "size_bytes, sha256, width, height, storage_path, created_at FROM attachments"
        )

    def create(self, record: AttachmentRecord) -> None:
        with self._lock:
            try:
                with self._connection.cursor() as cursor:
                    cursor.execute(
                        "INSERT INTO attachments "
                        "(id, workspace_id, tenant_id, user_id, filename, content_type, image_format, size_bytes, sha256, width, height, storage_path, created_at) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                        (
                            record.id,
                            record.workspace_id,
                            record.tenant_id,
                            record.user_id,
                            record.filename,
                            record.content_type,
                            record.image_format,
                            record.size_bytes,
                            record.sha256,
                            record.width,
                            record.height,
                            record.storage_path,
                            record.created_at,
                        ),
                    )
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise

    def get(
        self, attachment_id: str, *, workspace_id: str, tenant_id: str, user_id: str
    ) -> AttachmentRecord | None:
        with self._lock, self._connection.cursor() as cursor:
            cursor.execute(
                f"{self._select()} WHERE id = %s AND workspace_id = %s AND tenant_id = %s AND user_id = %s",
                (attachment_id, workspace_id, tenant_id, user_id),
            )
            row = cursor.fetchone()
        return self._from_row(row) if row else None

    def list(
        self, *, workspace_id: str, tenant_id: str, user_id: str
    ) -> Sequence[AttachmentRecord]:
        with self._lock, self._connection.cursor() as cursor:
            cursor.execute(
                f"{self._select()} WHERE workspace_id = %s AND tenant_id = %s AND user_id = %s ORDER BY created_at DESC",
                (workspace_id, tenant_id, user_id),
            )
            rows = cursor.fetchall()
        return tuple(self._from_row(row) for row in rows)

    def delete(
        self, attachment_id: str, *, workspace_id: str, tenant_id: str, user_id: str
    ) -> AttachmentRecord | None:
        record = self.get(
            attachment_id, workspace_id=workspace_id, tenant_id=tenant_id, user_id=user_id
        )
        if record is None:
            return None
        with self._lock:
            try:
                with self._connection.cursor() as cursor:
                    cursor.execute(
                        "DELETE FROM attachments WHERE id = %s AND workspace_id = %s AND tenant_id = %s AND user_id = %s",
                        (attachment_id, workspace_id, tenant_id, user_id),
                    )
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise
        return record

    def list_before(self, before: datetime) -> Sequence[AttachmentRecord]:
        with self._lock, self._connection.cursor() as cursor:
            cursor.execute(
                f"{self._select()} WHERE created_at < %s ORDER BY created_at",
                (before.astimezone(UTC).isoformat(),),
            )
            rows = cursor.fetchall()
        return tuple(self._from_row(row) for row in rows)
