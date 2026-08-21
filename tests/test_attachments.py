from __future__ import annotations

import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from typing import Self
from unittest.mock import patch

from fastapi.testclient import TestClient
from PIL import Image

from packages.agent_runtime import IdentityContext
from packages.attachments import (
    AttachmentError,
    AttachmentNotFound,
    AttachmentService,
    ClamAvScanner,
    BlobStorageError,
    MalwareDetected,
    S3BlobStore,
    SQLiteAttachmentStore,
)
from packages.vision import OpenAICompatibleVisionProvider, VisionService
from services.api.app import create_app
from services.bootstrap import build_runtime


def image_bytes(*, image_format: str = "PNG", size: tuple[int, int] = (4, 3)) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", size, (39, 94, 163)).save(buffer, format=image_format)
    return buffer.getvalue()


class FakeVisionProvider:
    provider_id = "fake-vision"
    model = "fake-vision-1"
    requires_external_consent = True

    def __init__(self) -> None:
        self.calls = 0

    def analyze(self, image_bytes: bytes, content_type: str, *, task: str, prompt: str) -> str:
        self.calls += 1
        return f"{task}: {content_type} image with {len(image_bytes)} bytes"


class FakeScanner:
    scanner_id = "fake-scanner"

    def __init__(self, *, reject: bool = False) -> None:
        self.reject = reject
        self.calls = 0

    def scan(self, data: bytes, content_type: str) -> None:
        self.calls += 1
        if self.reject:
            raise MalwareDetected("synthetic malware detection")


class FakeS3Body:
    def __init__(self, data: bytes) -> None:
        self.data = data

    def read(self, size: int) -> bytes:
        return self.data[:size]

    def close(self) -> None:
        return None


class FakeS3Client:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}
        self.put_arguments: dict[str, object] | None = None

    def put_object(self, **kwargs: object) -> None:
        self.put_arguments = kwargs
        self.objects[(str(kwargs["Bucket"]), str(kwargs["Key"]))] = bytes(kwargs["Body"])

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, FakeS3Body]:
        return {"Body": FakeS3Body(self.objects[(Bucket, Key)])}

    def delete_object(self, *, Bucket: str, Key: str) -> None:
        self.objects.pop((Bucket, Key), None)

    def head_bucket(self, *, Bucket: str) -> None:
        if Bucket != "copilot-bucket":
            raise RuntimeError("bucket not found")


class AttachmentServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.identity = IdentityContext("user-a", "workspace-a", "tenant-a", "customer")
        self.other_tenant = IdentityContext("user-a", "workspace-a", "tenant-b", "customer")
        self.service = AttachmentService(Path(self.directory.name) / "files", max_bytes=1024 * 1024)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_upload_validates_content_and_keeps_storage_tenant_scoped(self) -> None:
        record = self.service.upload(
            self.identity,
            filename="evidence.png",
            content_type="image/png",
            data=image_bytes(),
        )
        self.assertEqual((record.width, record.height, record.image_format), (4, 3, "PNG"))
        self.assertEqual(len(self.service.list(self.identity)), 1)
        content_record, path = self.service.content(self.identity, record.id)
        self.assertEqual(content_record.sha256, record.sha256)
        self.assertTrue(path.is_file())
        with self.assertRaises(AttachmentNotFound):
            self.service.metadata(self.other_tenant, record.id)

    def test_upload_rejects_spoofed_media_type_and_path_components(self) -> None:
        with self.assertRaises(AttachmentError):
            self.service.upload(
                self.identity,
                filename="evidence.jpg",
                content_type="image/jpeg",
                data=image_bytes(),
            )
        with self.assertRaises(AttachmentError):
            self.service.upload(
                self.identity,
                filename="..\\outside.png",
                content_type="image/png",
                data=image_bytes(),
            )

    def test_sqlite_metadata_survives_service_reopen(self) -> None:
        root = Path(self.directory.name) / "files"
        database = Path(self.directory.name) / "attachments.sqlite3"
        first = AttachmentService(root, store=SQLiteAttachmentStore(database))
        try:
            record = first.upload(
                self.identity,
                filename="persisted.webp",
                content_type="image/webp",
                data=image_bytes(image_format="WEBP"),
            )
        finally:
            first.close()
        reopened = AttachmentService(root, store=SQLiteAttachmentStore(database))
        try:
            loaded = reopened.metadata(self.identity, record.id)
            self.assertEqual(loaded.sha256, record.sha256)
            self.assertEqual(len(reopened.list(self.identity)), 1)
        finally:
            reopened.close()

    def test_scanner_runs_before_storage_and_rejection_leaves_no_record(self) -> None:
        scanner = FakeScanner(reject=True)
        service = AttachmentService(Path(self.directory.name) / "files", scanner=scanner)
        with self.assertRaises(MalwareDetected):
            service.upload(
                self.identity,
                filename="blocked.png",
                content_type="image/png",
                data=image_bytes(),
            )
        self.assertEqual(scanner.calls, 1)
        self.assertEqual(service.list(self.identity), ())

    def test_retention_cleanup_removes_expired_content_and_metadata(self) -> None:
        service = AttachmentService(Path(self.directory.name) / "files", retention_days=1)
        record = service.upload(
            self.identity,
            filename="retained.png",
            content_type="image/png",
            data=image_bytes(),
        )
        deleted = service.purge_expired(now=datetime.now(UTC) + timedelta(days=2))
        self.assertEqual([item.id for item in deleted], [record.id])
        self.assertEqual(service.list(self.identity), ())
        with self.assertRaises(AttachmentNotFound):
            service.metadata(self.identity, record.id)

    def test_s3_blob_adapter_encrypts_and_bounds_reads(self) -> None:
        client = FakeS3Client()
        blob_store = S3BlobStore(client, "copilot-bucket", prefix="tenant", kms_key_id="kms-key")
        blob_store.put("scope/image.png", b"abc", "image/png")
        self.assertEqual(client.put_arguments["ServerSideEncryption"], "aws:kms")
        self.assertEqual(client.put_arguments["SSEKMSKeyId"], "kms-key")
        self.assertEqual(blob_store.read("scope/image.png", 3), b"abc")
        with self.assertRaises(BlobStorageError):
            blob_store.read("scope/image.png", 2)
        blob_store.delete("scope/image.png")
        self.assertEqual(client.objects, {})
        self.assertTrue(blob_store.healthcheck())

    def test_attachment_health_checks_scanner_and_blob_store(self) -> None:
        service = AttachmentService(Path(self.directory.name) / "health")
        self.assertTrue(service.healthcheck())
        scanner = ClamAvScanner("clamav.example.test")
        connection = type(
            "Connection",
            (),
            {
                "__enter__": lambda self: self,
                "__exit__": lambda self, *args: None,
                "settimeout": lambda self, value: None,
                "sendall": lambda self, data: None,
                "recv": lambda self, size: b"PONG\0",
            },
        )()
        with patch(
            "packages.attachments.service.socket.create_connection", return_value=connection
        ):
            self.assertTrue(scanner.healthcheck())

    def test_attachment_service_can_use_s3_blob_boundary(self) -> None:
        client = FakeS3Client()
        service = AttachmentService(
            Path(self.directory.name) / "unused-local-root",
            blob_store=S3BlobStore(client, "copilot-bucket"),
        )
        record = service.upload(
            self.identity,
            filename="remote.png",
            content_type="image/png",
            data=image_bytes(),
        )
        loaded, data = service.read_content(self.identity, record.id)
        self.assertEqual((loaded.id, data), (record.id, image_bytes()))
        self.assertEqual(service.storage_mode, "s3")
        service.delete(self.identity, record.id)

    def test_migration_creates_attachment_table(self) -> None:
        database = Path(self.directory.name) / "migrated.sqlite3"
        import sys

        from scripts.migrate import main as migrate_main

        original_argv = sys.argv
        try:
            sys.argv = ["migrate.py", str(database)]
            self.assertEqual(migrate_main(), 0)
        finally:
            sys.argv = original_argv
        import sqlite3

        connection = sqlite3.connect(database)
        tables = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
            "('run_events', 'attachments', 'agent_runs', 'approval_requests') ORDER BY name"
        ).fetchall()
        connection.close()
        self.assertEqual(
            tables,
            [("agent_runs",), ("approval_requests",), ("attachments",), ("run_events",)],
        )


class AttachmentApiTests(unittest.TestCase):
    def test_http_upload_list_content_delete_and_audit(self) -> None:
        runtime, audit = build_runtime()
        with tempfile.TemporaryDirectory() as directory:
            service = AttachmentService(Path(directory) / "files")
            client = TestClient(
                create_app(runtime, audit, auth_mode="headers", attachments=service)
            )
            headers = {
                "X-User-Id": "demo-user",
                "X-Workspace-Id": "demo-workspace",
                "X-Tenant-Id": "demo-tenant",
                "X-Role": "customer",
            }
            uploaded = client.post(
                "/api/v1/attachments",
                headers=headers,
                files={"image": ("receipt.png", image_bytes(), "image/png")},
            )
            self.assertEqual(uploaded.status_code, 201, uploaded.text)
            payload = uploaded.json()
            self.assertEqual(payload["filename"], "receipt.png")
            self.assertNotIn("tenant_id", payload)

            listing = client.get("/api/v1/attachments", headers=headers)
            self.assertEqual(listing.status_code, 200)
            self.assertEqual(listing.json()["count"], 1)
            content = client.get(payload["content_url"], headers=headers)
            self.assertEqual(content.status_code, 200)
            self.assertEqual(content.headers["content-type"], "image/png")
            self.assertEqual(content.headers["x-content-type-options"], "nosniff")

            deleted = client.delete(f"/api/v1/attachments/{payload['id']}", headers=headers)
            self.assertEqual(deleted.status_code, 204)
            self.assertEqual(client.get(payload["content_url"], headers=headers).status_code, 404)
            self.assertEqual(
                [event.event_type for event in audit.events],
                ["attachment.created", "attachment.deleted"],
            )

    def test_vision_analysis_requires_consent_and_records_provenance(self) -> None:
        runtime, audit = build_runtime()
        with tempfile.TemporaryDirectory() as directory:
            service = AttachmentService(Path(directory) / "files")
            provider = FakeVisionProvider()
            vision = VisionService(provider)
            client = TestClient(
                create_app(runtime, audit, auth_mode="headers", attachments=service, vision=vision)
            )
            headers = {
                "X-User-Id": "demo-user",
                "X-Workspace-Id": "demo-workspace",
                "X-Tenant-Id": "demo-tenant",
                "X-Role": "customer",
            }
            uploaded = client.post(
                "/api/v1/attachments",
                headers=headers,
                files={"image": ("receipt.png", image_bytes(), "image/png")},
            )
            attachment_id = uploaded.json()["id"]
            missing_consent = client.post(
                f"/api/v1/attachments/{attachment_id}/analyze",
                headers=headers,
                json={"task": "ocr"},
            )
            self.assertEqual(missing_consent.status_code, 403)
            self.assertEqual(provider.calls, 0)
            analyzed = client.post(
                f"/api/v1/attachments/{attachment_id}/analyze",
                headers=headers,
                json={"task": "ocr", "allow_external_processing": True},
            )
            self.assertEqual(analyzed.status_code, 200, analyzed.text)
            self.assertIn("ocr:", analyzed.json()["text"])
            self.assertIn("attachment.analyzed", [event.event_type for event in audit.events])

    def test_openai_compatible_vision_adapter_sends_bounded_grounded_request(self) -> None:
        provider = OpenAICompatibleVisionProvider(
            "https://vision.example.test/v1",
            "vision-secret",
            "vision-model",
            allowed_hosts=["vision.example.test"],
        )
        captured: dict[str, object] = {}

        class Response:
            def __enter__(self) -> Self:
                return self

            def __exit__(self, *args: object) -> None:
                return None

            def read(self, _limit: int) -> bytes:
                return b'{"choices":[{"message":{"content":"ORDER 1001"}}]}'

        class Opener:
            def open(self, request: object, *, timeout: float) -> Response:
                captured["request"] = request
                captured["timeout"] = timeout
                return Response()

        with patch("urllib.request.build_opener", return_value=Opener()):
            text = provider.analyze(
                b"image-bytes",
                "image/png",
                task="ocr",
                prompt="Use only visible evidence.",
            )

        self.assertEqual(text, "ORDER 1001")
        outgoing = captured["request"]
        self.assertEqual(outgoing.get_header("Authorization"), "Bearer vision-secret")
        self.assertEqual(captured["timeout"], 30.0)
        payload = json.loads(outgoing.data)
        self.assertEqual(payload["model"], "vision-model")
        self.assertEqual(payload["temperature"], 0)
        self.assertIn(
            "data:image/png;base64,", payload["messages"][0]["content"][1]["image_url"]["url"]
        )
        with self.assertRaises(ValueError):
            OpenAICompatibleVisionProvider(
                "https://127.0.0.1/v1",
                "vision-secret",
                "vision-model",
                allowed_hosts=["127.0.0.1"],
            )

    def test_upload_rate_limit_returns_retry_after(self) -> None:
        runtime, audit = build_runtime()
        with tempfile.TemporaryDirectory() as directory:
            service = AttachmentService(Path(directory) / "files")
            client = TestClient(
                create_app(
                    runtime,
                    audit,
                    auth_mode="headers",
                    attachments=service,
                    upload_rate_limit=1,
                )
            )
            headers = {
                "X-User-Id": "demo-user",
                "X-Workspace-Id": "demo-workspace",
                "X-Tenant-Id": "demo-tenant",
                "X-Role": "customer",
            }
            first = client.post(
                "/api/v1/attachments",
                headers=headers,
                files={"image": ("one.png", image_bytes(), "image/png")},
            )
            second = client.post(
                "/api/v1/attachments",
                headers=headers,
                files={"image": ("two.png", image_bytes(), "image/png")},
            )
            self.assertEqual(first.status_code, 201)
            self.assertEqual(second.status_code, 429)
            self.assertTrue(second.headers.get("retry-after"))

    def test_multipart_size_guard_rejects_before_upload(self) -> None:
        runtime, audit = build_runtime()
        with tempfile.TemporaryDirectory() as directory:
            service = AttachmentService(Path(directory) / "files", max_bytes=1024)
            client = TestClient(
                create_app(runtime, audit, auth_mode="headers", attachments=service)
            )
            headers = {
                "X-User-Id": "demo-user",
                "X-Workspace-Id": "demo-workspace",
                "X-Tenant-Id": "demo-tenant",
                "X-Role": "customer",
                "Content-Length": "2000000",
            }
            response = client.post(
                "/api/v1/attachments",
                headers=headers,
                files={"image": ("oversized.png", image_bytes(), "image/png")},
            )
            self.assertEqual(response.status_code, 413)
            self.assertEqual(
                service.list(
                    IdentityContext("demo-user", "demo-workspace", "demo-tenant", "customer")
                ),
                (),
            )


if __name__ == "__main__":
    unittest.main()
