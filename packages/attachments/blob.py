"""Bounded blob-storage adapters for validated attachment bytes."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Protocol


class BlobStorageError(RuntimeError):
    """Raised when attachment bytes cannot be safely persisted or read."""


class BlobStore(Protocol):
    storage_id: str

    def put(self, key: str, data: bytes, content_type: str) -> None:
        """Persist a generated key and bounded content."""

    def read(self, key: str, max_bytes: int) -> bytes:
        """Read bounded content or raise when storage is unavailable."""

    def delete(self, key: str) -> None:
        """Delete a generated key."""


_SAFE_KEY = re.compile(r"^[A-Za-z0-9._/-]{1,512}$")


def _validate_key(key: str) -> str:
    normalized = key.replace("\\", "/")
    if (
        not _SAFE_KEY.fullmatch(normalized)
        or normalized.startswith("/")
        or any(part in {"", ".", ".."} for part in normalized.split("/"))
    ):
        raise BlobStorageError("storage key is invalid")
    return normalized


class FilesystemBlobStore:
    """Atomic, root-contained local blob adapter for development/single node."""

    storage_id = "filesystem"

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        candidate = (self.root / _validate_key(key)).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError as exc:
            raise BlobStorageError("storage key escaped configured root") from exc
        return candidate

    def put(self, key: str, data: bytes, content_type: str) -> None:
        del content_type
        target = self._path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.{os.urandom(12).hex()}.tmp")
        try:
            with temporary.open("xb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(target)
        except OSError as exc:
            temporary.unlink(missing_ok=True)
            raise BlobStorageError("filesystem blob write failed") from exc

    def read(self, key: str, max_bytes: int) -> bytes:
        path = self._path(key)
        if path.is_symlink() or not path.is_file():
            raise BlobStorageError("filesystem blob is unavailable")
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise BlobStorageError("filesystem blob read failed") from exc
        if len(data) > max_bytes:
            raise BlobStorageError("blob exceeds the configured read limit")
        return data

    def delete(self, key: str) -> None:
        path = self._path(key)
        if path.exists() and (path.is_symlink() or not path.is_file()):
            raise BlobStorageError("filesystem blob is not a regular file")
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            raise BlobStorageError("filesystem blob delete failed") from exc

    def local_path(self, key: str) -> Path:
        path = self._path(key)
        if path.is_symlink() or not path.is_file():
            raise BlobStorageError("filesystem blob is unavailable")
        return path

    def close(self) -> None:
        return None

    def healthcheck(self) -> bool:
        """Report whether the configured local blob root is available."""

        return self.root.is_dir()


class S3BlobStore:
    """S3-compatible adapter using an injected boto3 client.

    Credentials stay in the client factory/environment. Server-side encryption
    is requested for every object; KMS is preferred when a key id is supplied.
    """

    storage_id = "s3"

    def __init__(
        self,
        client: object,
        bucket: str,
        *,
        prefix: str = "attachments",
        kms_key_id: str | None = None,
    ) -> None:
        if not bucket or len(bucket) > 255 or any(character in bucket for character in "\r\n"):
            raise ValueError("object-storage bucket is invalid")
        clean_prefix = prefix.strip("/")
        if clean_prefix and not _SAFE_KEY.fullmatch(clean_prefix):
            raise ValueError("object-storage prefix is invalid")
        if kms_key_id is not None and (not kms_key_id.strip() or len(kms_key_id) > 512):
            raise ValueError("KMS key id is invalid")
        self.client = client
        self.bucket = bucket
        self.prefix = clean_prefix
        self.kms_key_id = kms_key_id.strip() if kms_key_id else None

    def _object_key(self, key: str) -> str:
        validated = _validate_key(key)
        return f"{self.prefix}/{validated}" if self.prefix else validated

    def _best_effort_delete(self, key: str) -> None:
        try:
            self.client.delete_object(Bucket=self.bucket, Key=self._object_key(key))  # type: ignore[attr-defined]
        except Exception:
            pass

    def put(self, key: str, data: bytes, content_type: str) -> None:
        arguments: dict[str, object] = {
            "Bucket": self.bucket,
            "Key": self._object_key(key),
            "Body": data,
            "ContentType": content_type,
            "ServerSideEncryption": "aws:kms" if self.kms_key_id else "AES256",
        }
        if self.kms_key_id:
            arguments["SSEKMSKeyId"] = self.kms_key_id
        written = False
        try:
            self.client.put_object(**arguments)  # type: ignore[attr-defined]
            written = True
            metadata = self.client.head_object(  # type: ignore[attr-defined]
                Bucket=self.bucket, Key=self._object_key(key)
            )
        except Exception as exc:
            if written:
                self._best_effort_delete(key)
            raise BlobStorageError("object-storage blob write failed") from exc
        expected_algorithm = "aws:kms" if self.kms_key_id else "AES256"
        if (
            not isinstance(metadata, dict)
            or metadata.get("ServerSideEncryption") != expected_algorithm
        ):
            self._best_effort_delete(key)
            raise BlobStorageError("object-storage encryption verification failed")
        if self.kms_key_id and not str(metadata.get("SSEKMSKeyId", "")).strip():
            self._best_effort_delete(key)
            raise BlobStorageError("object-storage KMS metadata is missing")

    def read(self, key: str, max_bytes: int) -> bytes:
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=self._object_key(key))  # type: ignore[attr-defined]
            body = response["Body"]
            data = body.read(max_bytes + 1)
            close = getattr(body, "close", None)
            if close is not None:
                close()
        except Exception as exc:
            raise BlobStorageError("object-storage blob read failed") from exc
        if len(data) > max_bytes:
            raise BlobStorageError("object-storage blob exceeds the read limit")
        return data

    def delete(self, key: str) -> None:
        try:
            self.client.delete_object(Bucket=self.bucket, Key=self._object_key(key))  # type: ignore[attr-defined]
        except Exception as exc:
            raise BlobStorageError("object-storage blob delete failed") from exc

    def close(self) -> None:
        return None

    def healthcheck(self) -> bool:
        """Check bucket reachability without reading or writing an object."""

        try:
            self.client.head_bucket(Bucket=self.bucket)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 - readiness must fail closed
            return False
        return True
