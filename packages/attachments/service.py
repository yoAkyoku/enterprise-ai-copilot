"""Safe, tenant-scoped image attachments.

The service deliberately owns both content validation and storage path
construction. Callers never supply a tenant path or a storage key. A future
object-storage adapter can implement the same repository boundary without
changing the API or policy layer.
"""

from __future__ import annotations

import hashlib
import re
import socket
import sqlite3
import threading
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from typing import Protocol

from PIL import Image, UnidentifiedImageError

from packages.agent_runtime.models import IdentityContext

from .blob import BlobStorageError, BlobStore, FilesystemBlobStore

ALLOWED_IMAGE_TYPES: dict[str, tuple[str, str]] = {
    "JPEG": ("image/jpeg", ".jpg"),
    "PNG": ("image/png", ".png"),
    "WEBP": ("image/webp", ".webp"),
    "GIF": ("image/gif", ".gif"),
}
DEFAULT_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_MAX_PIXELS = 25_000_000
_SAFE_IDENTITY = re.compile(r"^[A-Za-z0-9._:-]{1,256}$")


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise AttachmentError("attachment timestamp is invalid") from exc
    return (parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)).astimezone(UTC)


@dataclass(frozen=True)
class AttachmentRecord:
    id: str
    workspace_id: str
    tenant_id: str
    user_id: str
    filename: str
    content_type: str
    image_format: str
    size_bytes: int
    sha256: str
    width: int
    height: int
    storage_path: str
    created_at: str

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "workspace_id": self.workspace_id,
            "tenant_id": self.tenant_id,
            "user_id": self.user_id,
            "filename": self.filename,
            "content_type": self.content_type,
            "image_format": self.image_format,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "width": self.width,
            "height": self.height,
            "created_at": self.created_at,
        }


class AttachmentError(ValueError):
    """Raised for invalid or unsafe attachment input."""


class AttachmentNotFound(LookupError):
    """Raised when an attachment is outside the caller's scope or missing."""


class MalwareDetected(AttachmentError):
    """Raised when the configured malware scanner rejects an upload."""


class MalwareScanUnavailable(AttachmentError):
    """Raised when a required malware scanner cannot be reached."""


class AttachmentScanner(Protocol):
    scanner_id: str

    def scan(self, data: bytes, content_type: str) -> None:
        """Return only when content is safe; raise on detection or outage."""


class NoopAttachmentScanner:
    """Explicit development-only scanner that performs no malware inspection."""

    scanner_id = "disabled"

    def scan(self, data: bytes, content_type: str) -> None:
        del data, content_type

    def healthcheck(self) -> bool:
        return True


class ClamAvScanner:
    """Scan bytes with a local or private ClamAV ``clamd`` INSTREAM socket."""

    scanner_id = "clamav"

    def __init__(
        self, host: str = "127.0.0.1", port: int = 3310, *, timeout_seconds: float = 10.0
    ) -> None:
        if not host or len(host) > 255 or any(character in host for character in "\r\n"):
            raise ValueError("ClamAV host is invalid")
        if port < 1 or port > 65535:
            raise ValueError("ClamAV port is invalid")
        if timeout_seconds <= 0 or timeout_seconds > 60:
            raise ValueError("ClamAV timeout must be between 0 and 60 seconds")
        self.host = host
        self.port = port
        self.timeout_seconds = timeout_seconds

    def scan(self, data: bytes, content_type: str) -> None:
        del content_type
        try:
            with socket.create_connection(
                (self.host, self.port), timeout=self.timeout_seconds
            ) as connection:
                connection.settimeout(self.timeout_seconds)
                connection.sendall(b"zINSTREAM\0")
                for offset in range(0, len(data), 64 * 1024):
                    chunk = data[offset : offset + 64 * 1024]
                    connection.sendall(len(chunk).to_bytes(4, "big") + chunk)
                connection.sendall(b"\0\0\0\0")
                response = connection.recv(512)
        except (OSError, TimeoutError) as exc:
            raise MalwareScanUnavailable("malware scanner is unavailable") from exc
        try:
            message = response.decode("utf-8", errors="strict").strip()
        except UnicodeDecodeError as exc:
            raise MalwareScanUnavailable("malware scanner returned invalid data") from exc
        if not message.endswith("stream: OK"):
            if "FOUND" in message:
                raise MalwareDetected("malware scanner rejected the upload")
            raise MalwareScanUnavailable("malware scanner did not confirm the upload")

    def healthcheck(self) -> bool:
        """Use clamd's no-content PING command for readiness probes."""

        probe_timeout = min(self.timeout_seconds, 3.0)
        try:
            with socket.create_connection(
                (self.host, self.port), timeout=probe_timeout
            ) as connection:
                connection.settimeout(probe_timeout)
                connection.sendall(b"PING\0")
                response = connection.recv(16)
        except (OSError, TimeoutError):
            return False
        return response.rstrip(b"\0\r\n") == b"PONG"


class AttachmentStore(Protocol):
    def create(self, record: AttachmentRecord) -> None:
        """Persist metadata exactly once."""

    def get(
        self, attachment_id: str, *, workspace_id: str, tenant_id: str, user_id: str
    ) -> AttachmentRecord | None:
        """Return only a record in the supplied identity scope."""

    def list(
        self, *, workspace_id: str, tenant_id: str, user_id: str
    ) -> Sequence[AttachmentRecord]:
        """List records in the supplied identity scope."""

    def delete(
        self, attachment_id: str, *, workspace_id: str, tenant_id: str, user_id: str
    ) -> AttachmentRecord | None:
        """Delete and return one scoped record."""

    def list_before(self, before: datetime) -> Sequence[AttachmentRecord]:
        """List records older than the retention cutoff for maintenance."""


class InMemoryAttachmentStore:
    """Deterministic store for tests and ephemeral development."""

    def __init__(self) -> None:
        self._records: dict[str, AttachmentRecord] = {}

    def create(self, record: AttachmentRecord) -> None:
        if record.id in self._records:
            raise AttachmentError("attachment id already exists")
        self._records[record.id] = record

    def get(
        self, attachment_id: str, *, workspace_id: str, tenant_id: str, user_id: str
    ) -> AttachmentRecord | None:
        record = self._records.get(attachment_id)
        if record is None or (record.workspace_id, record.tenant_id, record.user_id) != (
            workspace_id,
            tenant_id,
            user_id,
        ):
            return None
        return record

    def list(
        self, *, workspace_id: str, tenant_id: str, user_id: str
    ) -> Sequence[AttachmentRecord]:
        return tuple(
            record
            for record in self._records.values()
            if (record.workspace_id, record.tenant_id, record.user_id)
            == (workspace_id, tenant_id, user_id)
        )

    def delete(
        self, attachment_id: str, *, workspace_id: str, tenant_id: str, user_id: str
    ) -> AttachmentRecord | None:
        record = self.get(
            attachment_id, workspace_id=workspace_id, tenant_id=tenant_id, user_id=user_id
        )
        if record is not None:
            del self._records[attachment_id]
        return record

    def list_before(self, before: datetime) -> Sequence[AttachmentRecord]:
        cutoff = before.astimezone(UTC)
        return tuple(
            record
            for record in self._records.values()
            if _parse_timestamp(record.created_at) < cutoff
        )


class SQLiteAttachmentStore:
    """Durable metadata store for a single API process or local deployment.

    The SQL is internal parameterized storage code; agents and MCP tools never
    receive a database connection or arbitrary SQL capability.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(self.path, check_same_thread=False, timeout=10)
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute(
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
            """
        )
        self._connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_attachments_scope "
            "ON attachments(workspace_id, tenant_id, user_id, created_at)"
        )
        self._connection.commit()

    @staticmethod
    def _from_row(row: tuple[object, ...]) -> AttachmentRecord:
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

    def create(self, record: AttachmentRecord) -> None:
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO attachments
                (id, workspace_id, tenant_id, user_id, filename, content_type,
                 image_format, size_bytes, sha256, width, height, storage_path, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
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

    def get(
        self, attachment_id: str, *, workspace_id: str, tenant_id: str, user_id: str
    ) -> AttachmentRecord | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT id, workspace_id, tenant_id, user_id, filename, content_type, image_format, "
                "size_bytes, sha256, width, height, storage_path, created_at FROM attachments "
                "WHERE id = ? AND workspace_id = ? AND tenant_id = ? AND user_id = ?",
                (attachment_id, workspace_id, tenant_id, user_id),
            ).fetchone()
        return self._from_row(row) if row else None

    def list(
        self, *, workspace_id: str, tenant_id: str, user_id: str
    ) -> Sequence[AttachmentRecord]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT id, workspace_id, tenant_id, user_id, filename, content_type, image_format, "
                "size_bytes, sha256, width, height, storage_path, created_at FROM attachments "
                "WHERE workspace_id = ? AND tenant_id = ? AND user_id = ? ORDER BY created_at DESC",
                (workspace_id, tenant_id, user_id),
            ).fetchall()
        return tuple(self._from_row(row) for row in rows)

    def delete(
        self, attachment_id: str, *, workspace_id: str, tenant_id: str, user_id: str
    ) -> AttachmentRecord | None:
        with self._lock:
            record = self.get(
                attachment_id, workspace_id=workspace_id, tenant_id=tenant_id, user_id=user_id
            )
            if record is None:
                return None
            self._connection.execute("DELETE FROM attachments WHERE id = ?", (attachment_id,))
            self._connection.commit()
        return record

    def list_before(self, before: datetime) -> Sequence[AttachmentRecord]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT id, workspace_id, tenant_id, user_id, filename, content_type, image_format, "
                "size_bytes, sha256, width, height, storage_path, created_at FROM attachments "
                "WHERE created_at < ? ORDER BY created_at",
                (before.astimezone(UTC).isoformat(),),
            ).fetchall()
        return tuple(self._from_row(row) for row in rows)

    def close(self) -> None:
        with self._lock:
            self._connection.close()


class AttachmentService:
    """Validate, persist and serve tenant-scoped images."""

    def __init__(
        self,
        root: str | Path,
        *,
        store: AttachmentStore | None = None,
        blob_store: BlobStore | None = None,
        scanner: AttachmentScanner | None = None,
        max_bytes: int = DEFAULT_MAX_BYTES,
        max_pixels: int = DEFAULT_MAX_PIXELS,
        retention_days: int = 0,
    ) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.store = store or InMemoryAttachmentStore()
        self.blob_store = blob_store or FilesystemBlobStore(self.root)
        self.scanner = scanner or NoopAttachmentScanner()
        self.max_bytes = max_bytes
        self.max_pixels = max_pixels
        if max_bytes < 1 or max_pixels < 1 or retention_days < 0:
            raise ValueError("attachment limits must be positive")
        self.retention_days = retention_days
        self.retention_seconds = retention_days * 86400
        self.requires_scan = self.scanner.scanner_id != "disabled"
        self.storage_mode = self.blob_store.storage_id

    def healthcheck(self) -> bool:
        """Fail readiness when metadata, blob storage or scanning is unavailable."""

        for component in (self.store, self.blob_store, self.scanner):
            healthcheck = getattr(component, "healthcheck", None)
            if healthcheck is None:
                continue
            try:
                if not bool(healthcheck()):
                    return False
            except Exception:  # noqa: BLE001 - readiness must fail closed
                return False
        return True

    @staticmethod
    def _scope_key(identity: IdentityContext) -> str:
        value = f"{identity.workspace_id}\0{identity.tenant_id}".encode()
        return hashlib.sha256(value).hexdigest()

    @staticmethod
    def _validate_identity(identity: IdentityContext) -> None:
        for field_name in ("user_id", "workspace_id", "tenant_id"):
            value = getattr(identity, field_name)
            if not value or not _SAFE_IDENTITY.fullmatch(value):
                raise AttachmentError(f"invalid {field_name} in authenticated context")

    def _path_for(self, record: AttachmentRecord) -> Path:
        candidate = (self.root / record.storage_path).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError as exc:
            raise AttachmentError("attachment storage path escaped configured root") from exc
        return candidate

    def _scoped_record(self, attachment_id: str, identity: IdentityContext) -> AttachmentRecord:
        self._validate_identity(identity)
        record = self.store.get(
            attachment_id,
            workspace_id=identity.workspace_id,
            tenant_id=identity.tenant_id,
            user_id=identity.user_id,
        )
        if record is None:
            raise AttachmentNotFound("attachment was not found")
        return record

    def upload(
        self, identity: IdentityContext, *, filename: str, content_type: str, data: bytes
    ) -> AttachmentRecord:
        self._validate_identity(identity)
        if (
            not filename
            or len(filename) > 255
            or any(ord(character) < 32 or ord(character) == 127 for character in filename)
            or Path(filename.replace("\\", "/")).name != filename
        ):
            raise AttachmentError("filename must be a single safe path component")
        if content_type not in {value[0] for value in ALLOWED_IMAGE_TYPES.values()}:
            raise AttachmentError("only JPEG, PNG, WebP and GIF images are supported")
        if not data:
            raise AttachmentError("image content is empty")
        if len(data) > self.max_bytes:
            raise AttachmentError(f"image exceeds the {self.max_bytes} byte limit")

        try:
            with Image.open(BytesIO(data)) as image:
                image.verify()
            with Image.open(BytesIO(data)) as image:
                image_format = image.format or ""
                width, height = image.size
        except (
            Image.DecompressionBombError,
            UnidentifiedImageError,
            OSError,
            SyntaxError,
            ValueError,
        ) as exc:
            raise AttachmentError("file content is not a valid image") from exc

        image_info = ALLOWED_IMAGE_TYPES.get(image_format)
        if image_info is None or image_info[0] != content_type:
            raise AttachmentError("declared content type does not match image content")
        if width < 1 or height < 1 or width * height > self.max_pixels:
            raise AttachmentError(f"image dimensions exceed the {self.max_pixels} pixel limit")
        self.scanner.scan(data, content_type)

        attachment_id = uuid.uuid4().hex
        scope_key = self._scope_key(identity)
        extension = image_info[1]
        relative_path = f"{scope_key}/{attachment_id}{extension}"
        record = AttachmentRecord(
            id=attachment_id,
            workspace_id=identity.workspace_id,
            tenant_id=identity.tenant_id,
            user_id=identity.user_id,
            filename=filename,
            content_type=content_type,
            image_format=image_format,
            size_bytes=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            width=width,
            height=height,
            storage_path=relative_path,
            created_at=_utc_now(),
        )
        try:
            self.blob_store.put(record.storage_path, data, content_type)
            self.store.create(record)
        except BlobStorageError as exc:
            raise AttachmentError("attachment content could not be stored") from exc
        except Exception:
            try:
                self.blob_store.delete(record.storage_path)
            except BlobStorageError:
                pass
            raise
        return record

    def list(self, identity: IdentityContext) -> Sequence[AttachmentRecord]:
        self._validate_identity(identity)
        return self.store.list(
            workspace_id=identity.workspace_id,
            tenant_id=identity.tenant_id,
            user_id=identity.user_id,
        )

    def metadata(self, identity: IdentityContext, attachment_id: str) -> AttachmentRecord:
        return self._scoped_record(attachment_id, identity)

    def content(
        self, identity: IdentityContext, attachment_id: str
    ) -> tuple[AttachmentRecord, Path]:
        record = self._scoped_record(attachment_id, identity)
        local_path = getattr(self.blob_store, "local_path", None)
        if local_path is None:
            raise AttachmentError("object-storage content must be read through the service")
        try:
            path = local_path(record.storage_path)
        except BlobStorageError:
            raise AttachmentNotFound("attachment content is unavailable")
        return record, path

    def read_content(
        self, identity: IdentityContext, attachment_id: str
    ) -> tuple[AttachmentRecord, bytes]:
        record = self._scoped_record(attachment_id, identity)
        try:
            data = self.blob_store.read(record.storage_path, self.max_bytes)
        except BlobStorageError as exc:
            raise AttachmentNotFound("attachment content is unavailable") from exc
        if len(data) != record.size_bytes or hashlib.sha256(data).hexdigest() != record.sha256:
            raise AttachmentError("attachment content failed integrity verification")
        return record, data

    def delete(self, identity: IdentityContext, attachment_id: str) -> AttachmentRecord:
        record = self._scoped_record(attachment_id, identity)
        try:
            self.blob_store.delete(record.storage_path)
            deleted = self.store.delete(
                attachment_id,
                workspace_id=identity.workspace_id,
                tenant_id=identity.tenant_id,
                user_id=identity.user_id,
            )
        except BlobStorageError as exc:
            raise AttachmentError("attachment could not be deleted") from exc
        if deleted is None:
            raise AttachmentNotFound("attachment was not found")
        return deleted

    def purge_expired(self, *, now: datetime | None = None) -> tuple[AttachmentRecord, ...]:
        """Delete content and metadata older than the configured retention period."""

        if self.retention_seconds <= 0:
            return ()
        current = (now or datetime.now(UTC)).astimezone(UTC)
        cutoff = current - timedelta(seconds=self.retention_seconds)
        expired = tuple(self.store.list_before(cutoff))
        deleted: list[AttachmentRecord] = []
        for record in expired:
            try:
                self.blob_store.delete(record.storage_path)
                removed = self.store.delete(
                    record.id,
                    workspace_id=record.workspace_id,
                    tenant_id=record.tenant_id,
                    user_id=record.user_id,
                )
            except BlobStorageError as exc:
                raise AttachmentError("expired attachment could not be deleted") from exc
            if removed is not None:
                deleted.append(removed)
        return tuple(deleted)

    def close(self) -> None:
        """Close a durable metadata adapter when the host is shutting down."""

        close = getattr(self.store, "close", None)
        if close is not None:
            close()
        close_blob = getattr(self.blob_store, "close", None)
        if close_blob is not None:
            close_blob()
