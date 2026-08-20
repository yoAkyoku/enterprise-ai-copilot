"""Durable, scoped approval records for high-risk tool actions."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
import threading
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

from .models import IdentityContext, ToolRisk


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _scope(identity: IdentityContext) -> tuple[str, str]:
    if not all(
        isinstance(value, str) and value.strip() and len(value) <= 256
        for value in (identity.user_id, identity.workspace_id, identity.tenant_id, identity.role)
    ):
        raise ApprovalError("authenticated identity scope is invalid")
    return identity.workspace_id, identity.tenant_id


def _arguments_digest(arguments: Mapping[str, object]) -> tuple[str, str]:
    try:
        canonical = json.dumps(arguments, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise ApprovalError("approval arguments must be JSON serializable") from exc
    if len(canonical.encode("utf-8")) > 100_000:
        raise ApprovalError("approval arguments are too large")
    return canonical, hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ApprovalError(ValueError):
    """Raised for invalid or disallowed approval operations."""


class ApprovalNotFound(LookupError):
    """Raised when an approval is outside the caller scope or unavailable."""


class ApprovalStatus:
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


@dataclass(frozen=True)
class ApprovalRecord:
    id: str
    requester: IdentityContext
    tool_name: str
    arguments: dict[str, object]
    arguments_hash: str
    risk: ToolRisk
    idempotency_key: str | None
    requested_at: str
    expires_at: str
    status: str
    approver_user_id: str | None = None
    decided_at: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "tool_name": self.tool_name,
            "arguments": self.arguments,
            "arguments_hash": self.arguments_hash,
            "risk": self.risk.value,
            "status": self.status,
            "requested_at": self.requested_at,
            "expires_at": self.expires_at,
            "approver_user_id": self.approver_user_id,
            "decided_at": self.decided_at,
        }


class ApprovalStore(Protocol):
    def create(self, record: ApprovalRecord) -> None:
        """Persist a pending approval."""

    def get(self, approval_id: str, *, workspace_id: str, tenant_id: str) -> ApprovalRecord | None:
        """Return only records in the supplied workspace and tenant."""

    def list(
        self, *, workspace_id: str, tenant_id: str, status: str | None = None
    ) -> Sequence[ApprovalRecord]:
        """List scoped records."""

    def find_idempotent(
        self, *, workspace_id: str, tenant_id: str, user_id: str, idempotency_key: str
    ) -> ApprovalRecord | None:
        """Find a request created by the same identity and key."""

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
        """Atomically transition a pending record."""

    def close(self) -> None:
        """Close a durable store."""


class InMemoryApprovalStore:
    def __init__(self) -> None:
        self._records: dict[str, ApprovalRecord] = {}
        self._token_hashes: dict[str, str] = {}
        self._lock = threading.RLock()

    def create(self, record: ApprovalRecord) -> None:
        with self._lock:
            if record.id in self._records:
                raise ApprovalError("approval id already exists")
            self._records[record.id] = record

    def get(self, approval_id: str, *, workspace_id: str, tenant_id: str) -> ApprovalRecord | None:
        with self._lock:
            record = self._records.get(approval_id)
            if record is None or (record.requester.workspace_id, record.requester.tenant_id) != (
                workspace_id,
                tenant_id,
            ):
                return None
            return record

    def list(
        self, *, workspace_id: str, tenant_id: str, status: str | None = None
    ) -> Sequence[ApprovalRecord]:
        with self._lock:
            return tuple(
                record
                for record in self._records.values()
                if (record.requester.workspace_id, record.requester.tenant_id)
                == (workspace_id, tenant_id)
                and (status is None or record.status == status)
            )

    def find_idempotent(
        self, *, workspace_id: str, tenant_id: str, user_id: str, idempotency_key: str
    ) -> ApprovalRecord | None:
        with self._lock:
            return next(
                (
                    record
                    for record in self._records.values()
                    if (
                        record.requester.workspace_id,
                        record.requester.tenant_id,
                        record.requester.user_id,
                    )
                    == (workspace_id, tenant_id, user_id)
                    and record.idempotency_key == idempotency_key
                ),
                None,
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
            record = self.get(approval_id, workspace_id=workspace_id, tenant_id=tenant_id)
            if record is None or record.status != ApprovalStatus.PENDING:
                return None
            updated = ApprovalRecord(
                **{
                    **record.__dict__,
                    "status": status,
                    "approver_user_id": approver_user_id,
                    "decided_at": decided_at,
                }
            )
            self._records[approval_id] = updated
            if token_hash:
                self._token_hashes[approval_id] = token_hash
            return updated

    def token_hash(self, approval_id: str, *, workspace_id: str, tenant_id: str) -> str | None:
        with self._lock:
            if self.get(approval_id, workspace_id=workspace_id, tenant_id=tenant_id) is None:
                return None
            return self._token_hashes.get(approval_id)

    def close(self) -> None:
        return None


class SQLiteApprovalStore:
    """SQLite adapter for local/single-process deployments."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(self.path, check_same_thread=False, timeout=10)
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute(
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
            """
        )
        self._connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_approval_scope ON approval_requests(workspace_id, tenant_id, status, requested_at)"
        )
        self._connection.commit()

    @staticmethod
    def _from_row(row: tuple[object, ...]) -> ApprovalRecord:
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
                self._connection.execute(
                    "INSERT INTO approval_requests "
                    "(id, workspace_id, tenant_id, requester_user_id, requester_role, tool_name, arguments_json, "
                    "arguments_hash, risk, idempotency_key, requested_at, expires_at, status) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
            except sqlite3.IntegrityError as exc:
                raise ApprovalError("approval id or idempotency key already exists") from exc

    def get(self, approval_id: str, *, workspace_id: str, tenant_id: str) -> ApprovalRecord | None:
        with self._lock:
            row = self._connection.execute(
                f"{self._select()} WHERE id = ? AND workspace_id = ? AND tenant_id = ?",
                (approval_id, workspace_id, tenant_id),
            ).fetchone()
        return self._from_row(row) if row else None

    def list(
        self, *, workspace_id: str, tenant_id: str, status: str | None = None
    ) -> Sequence[ApprovalRecord]:
        clauses = ["workspace_id = ?", "tenant_id = ?"]
        values: list[object] = [workspace_id, tenant_id]
        if status is not None:
            clauses.append("status = ?")
            values.append(status)
        with self._lock:
            rows = self._connection.execute(
                f"{self._select()} WHERE {' AND '.join(clauses)} ORDER BY requested_at DESC", values
            ).fetchall()
        return tuple(self._from_row(row) for row in rows)

    def find_idempotent(
        self, *, workspace_id: str, tenant_id: str, user_id: str, idempotency_key: str
    ) -> ApprovalRecord | None:
        with self._lock:
            row = self._connection.execute(
                f"{self._select()} WHERE workspace_id = ? AND tenant_id = ? AND requester_user_id = ? "
                "AND idempotency_key = ?",
                (workspace_id, tenant_id, user_id, idempotency_key),
            ).fetchone()
        return self._from_row(row) if row else None

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
            cursor = self._connection.execute(
                "UPDATE approval_requests SET status = ?, approver_user_id = ?, decided_at = ?, token_hash = ? "
                "WHERE id = ? AND workspace_id = ? AND tenant_id = ? AND status = ?",
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
            self._connection.commit()
            if cursor.rowcount != 1:
                return None
        return self.get(approval_id, workspace_id=workspace_id, tenant_id=tenant_id)

    def token_hash(self, approval_id: str, *, workspace_id: str, tenant_id: str) -> str | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT token_hash FROM approval_requests WHERE id = ? AND workspace_id = ? AND tenant_id = ?",
                (approval_id, workspace_id, tenant_id),
            ).fetchone()
        return str(row[0]) if row and row[0] else None

    def close(self) -> None:
        with self._lock:
            self._connection.close()


class ApprovalService:
    """Create and resolve approvals without exposing raw tokens at rest."""

    def __init__(self, store: ApprovalStore | None = None) -> None:
        self.store = store or InMemoryApprovalStore()

    def request(
        self,
        requester: IdentityContext,
        *,
        tool_name: str,
        arguments: Mapping[str, object],
        risk: ToolRisk,
        idempotency_key: str | None = None,
        ttl_seconds: int = 900,
    ) -> ApprovalRecord:
        _scope(requester)
        if not tool_name or len(tool_name) > 256 or risk is ToolRisk.READ:
            raise ApprovalError("only registered high-risk operations require approval")
        if idempotency_key is not None and (
            not idempotency_key.strip() or len(idempotency_key) > 200
        ):
            raise ApprovalError("idempotency key must be 1-200 characters")
        if ttl_seconds < 60 or ttl_seconds > 86400:
            raise ApprovalError("approval TTL must be between 60 and 86400 seconds")
        if idempotency_key:
            existing = self.store.find_idempotent(
                workspace_id=requester.workspace_id,
                tenant_id=requester.tenant_id,
                user_id=requester.user_id,
                idempotency_key=idempotency_key,
            )
            if existing is not None:
                return existing
        canonical, digest = _arguments_digest(arguments)
        requested_at = _now()
        record = ApprovalRecord(
            id=uuid.uuid4().hex,
            requester=requester,
            tool_name=tool_name,
            arguments=json.loads(canonical),
            arguments_hash=digest,
            risk=risk,
            idempotency_key=idempotency_key,
            requested_at=_iso(requested_at),
            expires_at=_iso(requested_at + timedelta(seconds=ttl_seconds)),
            status=ApprovalStatus.PENDING,
        )
        self.store.create(record)
        return record

    def list(
        self, identity: IdentityContext, *, status: str | None = None
    ) -> Sequence[ApprovalRecord]:
        _scope(identity)
        return self.store.list(
            workspace_id=identity.workspace_id, tenant_id=identity.tenant_id, status=status
        )

    def get(self, identity: IdentityContext, approval_id: str) -> ApprovalRecord:
        _scope(identity)
        record = self.store.get(
            approval_id, workspace_id=identity.workspace_id, tenant_id=identity.tenant_id
        )
        if record is None:
            raise ApprovalNotFound("approval was not found")
        return self._expire_if_needed(record)

    def approve(self, approver: IdentityContext, approval_id: str) -> tuple[ApprovalRecord, str]:
        _scope(approver)
        if approver.role not in {"admin", "manager"}:
            raise ApprovalError("only manager or admin can approve high-risk actions")
        record = self.get(approver, approval_id)
        if record.status != ApprovalStatus.PENDING:
            raise ApprovalError("approval is no longer pending")
        token = secrets.token_urlsafe(32)
        updated = self.store.decide(
            approval_id,
            workspace_id=approver.workspace_id,
            tenant_id=approver.tenant_id,
            status=ApprovalStatus.APPROVED,
            approver_user_id=approver.user_id,
            decided_at=_iso(_now()),
            token_hash=self._hash_token(token),
        )
        if updated is None:
            raise ApprovalError("approval changed before it could be approved")
        return updated, token

    def reject(self, approver: IdentityContext, approval_id: str) -> ApprovalRecord:
        _scope(approver)
        if approver.role not in {"admin", "manager"}:
            raise ApprovalError("only manager or admin can reject high-risk actions")
        record = self.get(approver, approval_id)
        if record.status != ApprovalStatus.PENDING:
            raise ApprovalError("approval is no longer pending")
        updated = self.store.decide(
            approval_id,
            workspace_id=approver.workspace_id,
            tenant_id=approver.tenant_id,
            status=ApprovalStatus.REJECTED,
            approver_user_id=approver.user_id,
            decided_at=_iso(_now()),
            token_hash=None,
        )
        if updated is None:
            raise ApprovalError("approval changed before it could be rejected")
        return updated

    def verify(
        self,
        identity: IdentityContext,
        approval_id: str,
        token: str,
        *,
        tool_name: str,
        arguments: Mapping[str, object],
    ) -> bool:
        record = self.get(identity, approval_id)
        if record.status != ApprovalStatus.APPROVED or record.tool_name != tool_name:
            return False
        _, digest = _arguments_digest(arguments)
        if not hmac.compare_digest(record.arguments_hash, digest):
            return False
        token_hash = self._stored_token_hash(approval_id, identity)
        return token_hash is not None and hmac.compare_digest(token_hash, self._hash_token(token))

    def pending_count(self, identity: IdentityContext) -> int:
        return sum(
            1
            for record in self.list(identity, status=ApprovalStatus.PENDING)
            if self._not_expired(record)
        )

    def close(self) -> None:
        self.store.close()

    def _expire_if_needed(self, record: ApprovalRecord) -> ApprovalRecord:
        if record.status == ApprovalStatus.PENDING and not self._not_expired(record):
            updated = self.store.decide(
                record.id,
                workspace_id=record.requester.workspace_id,
                tenant_id=record.requester.tenant_id,
                status=ApprovalStatus.EXPIRED,
                approver_user_id=None,
                decided_at=_iso(_now()),
                token_hash=None,
            )
            return updated or record
        return record

    @staticmethod
    def _not_expired(record: ApprovalRecord) -> bool:
        return datetime.fromisoformat(record.expires_at) > _now()

    def _stored_token_hash(self, approval_id: str, identity: IdentityContext) -> str | None:
        getter = getattr(self.store, "token_hash", None)
        if getter is None:
            return None
        return getter(approval_id, workspace_id=identity.workspace_id, tenant_id=identity.tenant_id)

    @staticmethod
    def _hash_token(token: str) -> str:
        if not token or len(token) > 512:
            return ""
        return hashlib.sha256(token.encode("utf-8")).hexdigest()
