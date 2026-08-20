from __future__ import annotations

import json
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from typing import Self
from unittest.mock import patch

from fastapi.testclient import TestClient

from packages.agent_runtime import (
    AuthenticationError,
    IdentityContext,
    InMemoryMcpGateway,
    JwtHs256Authenticator,
    JwtJwksAuthenticator,
    PolicyEngine,
    RedisRateLimiter,
    SqliteAuditStore,
    StreamableHttpMcpGateway,
    ToolDefinition,
    ToolRisk,
)
from packages.agent_runtime.models import AuditEvent, ToolCallRequest
from packages.contracts import validate_agent_manifest, validate_plugin, validate_repository
from packages.plugins import PluginInstallError, PluginRegistry
from packages.scheduler import (
    InMemoryJobQueue,
    RedisJobQueue,
    ScheduleDefinition,
    Scheduler,
    ScheduleStatus,
)
from services.api.app import create_app
from services.bootstrap import build_runtime

ROOT = Path(__file__).resolve().parents[1]


class PlatformContractTests(unittest.TestCase):
    def test_repository_contracts_validate(self) -> None:
        reports = validate_repository(ROOT)
        self.assertGreaterEqual(len(reports), 5)
        self.assertTrue(all(report.valid for report in reports), reports)

    def test_mcp_health_is_explicit(self) -> None:
        gateway = InMemoryMcpGateway({})
        self.assertEqual(
            gateway.health(),
            {"server_id": "erp-demo", "transport": "in_memory", "status": "healthy"},
        )

    def test_remote_mcp_endpoint_requires_https_and_host_allowlist(self) -> None:
        gateway = InMemoryMcpGateway({})
        with self.assertRaises(ValueError):
            StreamableHttpMcpGateway(
                "http://mcp.example.test/tools",
                gateway.definitions,
                allowed_hosts=["mcp.example.test"],
            )
        configured = StreamableHttpMcpGateway(
            "https://mcp.example.test/tools",
            gateway.definitions,
            allowed_hosts=["mcp.example.test"],
        )
        self.assertEqual(configured.transport, "streamable_http")
        configured_port = StreamableHttpMcpGateway(
            "https://mcp.example.test:8443/tools",
            gateway.definitions,
            allowed_hosts=["mcp.example.test"],
        )
        self.assertEqual(configured_port.endpoint, "https://mcp.example.test:8443/tools")
        with self.assertRaises(ValueError):
            StreamableHttpMcpGateway(
                "https://127.0.0.1/tools",
                gateway.definitions,
                allowed_hosts=["127.0.0.1"],
            )

    def test_remote_mcp_call_preserves_scope_headers_and_provenance(self) -> None:
        gateway = StreamableHttpMcpGateway(
            "https://mcp.example.test/tools",
            InMemoryMcpGateway({}).definitions,
            allowed_hosts=["mcp.example.test"],
            bearer_token="connector-secret",
        )
        captured: dict[str, object] = {}

        class Response:
            status = 200

            def __enter__(self) -> Self:
                return self

            def __exit__(self, *args: object) -> None:
                return None

            def read(self, _limit: int) -> bytes:
                return json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "result": {
                            "structuredContent": {
                                "data": {"status": "in_transit"},
                                "source_id": "erp:SO-1001",
                                "observed_at": "2026-08-20T00:00:00+00:00",
                                "external_ref": "SO-1001",
                            }
                        },
                    }
                ).encode()

        class Opener:
            def open(self, request: object, *, timeout: float) -> Response:
                captured["request"] = request
                captured["timeout"] = timeout
                return Response()

        request = ToolCallRequest(
            request_id="request-1",
            trace_id="trace-1",
            run_id="run-1",
            identity=IdentityContext("user-a", "workspace-a", "tenant-a", "customer"),
            tool_name="erp.get_order_status",
            arguments={"order_id": "SO-1001"},
            idempotency_key="run-1:order-status",
        )
        with patch("urllib.request.build_opener", return_value=Opener()):
            result = gateway.call(request)

        self.assertTrue(result.success)
        self.assertEqual(result.external_ref, "SO-1001")
        outgoing = captured["request"]
        self.assertEqual(outgoing.get_header("Authorization"), "Bearer connector-secret")
        outgoing_headers = {key.lower(): value for key, value in outgoing.header_items()}
        self.assertEqual(outgoing_headers["x-tenant-id"], "tenant-a")
        self.assertEqual(outgoing_headers["x-workspace-id"], "workspace-a")
        payload = json.loads(outgoing.data)
        self.assertEqual(payload["params"]["arguments"], {"order_id": "SO-1001"})
        self.assertEqual(captured["timeout"], 10.0)

    def test_policy_role_matrix_is_explicit(self) -> None:
        policy = PolicyEngine(
            {
                "erp.get_order_status": ToolDefinition(
                    name="erp.get_order_status", risk=ToolRisk.READ, description="test"
                )
            }
        )
        allowed_roles = ("customer", "support", "sales", "manager", "admin")
        for role in allowed_roles:
            decision = policy.authorize(
                IdentityContext("user", "workspace", "tenant", role), "erp.get_order_status"
            )
            self.assertEqual(decision.outcome, "allow", role)
        denied = policy.authorize(
            IdentityContext("user", "workspace", "tenant", "unknown"), "erp.get_order_status"
        )
        self.assertEqual(denied.outcome, "deny")

    def test_ci_actions_are_pinned_to_immutable_shas(self) -> None:
        action_lines: list[str] = []
        for workflow_path in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
            workflow = workflow_path.read_text(encoding="utf-8")
            action_lines.extend(
                line.strip() for line in workflow.splitlines() if line.strip().startswith("uses:")
            )
        self.assertGreaterEqual(len(action_lines), 5)
        self.assertTrue(
            all(re.search(r"@[0-9a-f]{40}", line) for line in action_lines), action_lines
        )

    def test_agent_instruction_path_traversal_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "AGENT.md").write_text("safe\n", encoding="utf-8")
            manifest = root / "agent.yaml"
            manifest.write_text(
                """
id: unsafe-agent
version: 0.1.0
name: Unsafe
description: Test
instructions: ../outside.md
approval:
  read: auto
  write: required
  external_send: required
  destructive: deny
limits:
  max_steps: 1
  max_runtime_seconds: 1
""",
                encoding="utf-8",
            )
            report = validate_agent_manifest(manifest)
            self.assertFalse(report.valid)
            self.assertTrue(
                any("relative path" in issue or "inside" in issue for issue in report.issues)
            )

    def test_plugin_reserved_name_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "plugin"
            (root / ".codex-plugin").mkdir(parents=True)
            (root / ".codex-plugin" / "plugin.json").write_text(
                json.dumps(
                    {
                        "name": ".rollback",
                        "version": "0.1.0",
                        "description": "unsafe",
                        "publisher": "test",
                        "skills": ".codex-plugin",
                        "mcp": ".codex-plugin",
                        "permissions": {"network": [], "tools": []},
                    }
                ),
                encoding="utf-8",
            )
            report = validate_plugin(root)
            self.assertFalse(report.valid)
            self.assertTrue(any("name must contain" in issue for issue in report.issues))

    def test_sqlite_audit_events_survive_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "audit.sqlite3"
            store = SqliteAuditStore(path)
            event = AuditEvent(
                event_type="run.created",
                request_id="req-1",
                trace_id="trace-1",
                run_id="run-1",
                workspace_id="workspace-1",
                agent_id="agent-1",
                payload={"synthetic": True},
            )
            store.append(event)
            store.close()
            reopened = SqliteAuditStore(path)
            events = reopened.list_events(run_id="run-1")
            reopened.close()
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].payload, {"synthetic": True})

    def test_sqlite_audit_store_creates_nested_data_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SqliteAuditStore(Path(directory) / "nested" / "audit.sqlite3")
            store.close()
            self.assertTrue((Path(directory) / "nested" / "audit.sqlite3").is_file())

    def test_checked_in_migration_creates_run_events_table(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "migrated.sqlite3"
            completed = subprocess.run(
                [sys.executable, str(ROOT / "scripts" / "migrate.py"), str(database)],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            connection = sqlite3.connect(database)
            tables = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='run_events'"
            ).fetchall()
            connection.close()
            self.assertEqual(tables, [("run_events",)])

    def test_sqlite_backup_and_restore_integrity_check(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.sqlite3"
            backup = Path(directory) / "backup" / "platform.sqlite3"
            subprocess.run(
                [sys.executable, str(ROOT / "scripts" / "migrate.py"), str(source)],
                check=True,
                capture_output=True,
                text=True,
            )
            from scripts.backup_sqlite import backup_database, verify_database

            self.assertEqual(backup_database(source, backup), 0)
            self.assertEqual(verify_database(backup), (True, "ok"))

    def test_scheduler_is_idempotent_and_retries(self) -> None:
        definition = ScheduleDefinition(
            id="test-schedule",
            version="0.1.0",
            agent="customer-service-agent",
            schedule_type="one_shot",
            at="2030-01-01T09:00:00+08:00",
            timezone_name="Asia/Taipei",
            retry_limit=1,
            permissions_mode="read_only",
        )
        scheduler = Scheduler()
        scheduler.register(definition)
        attempts = 0

        def execute(_definition: ScheduleDefinition, _key: str) -> str:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("synthetic transient failure")
            return "completed"

        trigger_time = datetime(2030, 1, 1, 2, 0, tzinfo=UTC)
        first = scheduler.trigger(definition.id, trigger_time, execute)
        second = scheduler.trigger(definition.id, trigger_time, execute)
        self.assertEqual(first.status, ScheduleStatus.SUCCEEDED)
        self.assertEqual(first.attempts, 2)
        self.assertEqual(first, second)
        self.assertEqual(attempts, 2)

    def test_scheduler_notifies_findings_once_and_honors_cancellation(self) -> None:
        notifications: list[tuple[str, str, str]] = []

        class Sink:
            def send(self, *, channel, schedule, run) -> None:  # type: ignore[no-untyped-def]
                notifications.append((channel, schedule.id, run.status.value))

        finding_definition = ScheduleDefinition(
            id="finding-schedule",
            version="0.1.0",
            agent="customer-service-agent",
            schedule_type="one_shot",
            at="2030-01-01T09:00:00+08:00",
            timezone_name="Asia/Taipei",
            notify_channel="web",
            notify_only_if="finding_or_failure",
        )
        scheduler = Scheduler(notifier=Sink())
        scheduler.register(finding_definition)
        trigger_time = datetime(2030, 1, 1, 2, 0, tzinfo=UTC)
        first = scheduler.trigger(
            finding_definition.id,
            trigger_time,
            lambda _definition, _key: "finding",
            finding=True,
        )
        duplicate = scheduler.trigger(
            finding_definition.id,
            trigger_time,
            lambda _definition, _key: "duplicate",
            finding=True,
        )
        self.assertTrue(first.notification_sent)
        self.assertEqual(first, duplicate)
        self.assertEqual(notifications, [("web", "finding-schedule", "succeeded")])

        cancelled_definition = ScheduleDefinition(
            id="cancelled-schedule",
            version="0.1.0",
            agent="customer-service-agent",
            schedule_type="one_shot",
            at="2030-01-01T09:00:00+08:00",
            timezone_name="Asia/Taipei",
        )
        scheduler.register(cancelled_definition)
        cancelled_key = "cancelled-schedule:2030-01-01T01:00:00+00:00"
        scheduler.cancel(cancelled_key)
        called = False

        def should_not_run(_definition, _key):  # type: ignore[no-untyped-def]
            nonlocal called
            called = True
            return "unexpected"

        cancelled = scheduler.trigger(cancelled_definition.id, trigger_time, should_not_run)
        self.assertEqual(cancelled.status, ScheduleStatus.CANCELLED)
        self.assertFalse(called)

    def test_job_queue_preserves_payload_and_ack_boundary(self) -> None:
        queue = InMemoryJobQueue()
        job = queue.enqueue({"schedule_id": "schedule-1", "tenant_id": "tenant-a"})
        received = queue.receive()
        self.assertEqual(received, job)
        self.assertEqual(received.payload["tenant_id"], "tenant-a")
        queue.ack(received)
        self.assertIsNone(queue.receive())

    def test_redis_decode_normalizes_bytes_stream_receipt(self) -> None:
        job = RedisJobQueue._decode(
            (
                b"1740000000000-0",
                {
                    b"job_id": b"job-1",
                    b"payload": b'{"trace_id":"trace-1"}',
                    b"enqueued_at": b"1740000000.0",
                },
            )
        )
        self.assertEqual(job.receipt, "1740000000000-0")
        self.assertEqual(job.id, "job-1")
        self.assertEqual(job.payload, {"trace_id": "trace-1"})

    def test_scheduler_blocks_unapproved_write_mode(self) -> None:
        definition = ScheduleDefinition(
            id="write-schedule",
            version="0.1.0",
            agent="customer-service-agent",
            schedule_type="one_shot",
            at="2030-01-01T09:00:00+08:00",
            timezone_name="Asia/Taipei",
            permissions_mode="approved_write",
        )
        scheduler = Scheduler()
        scheduler.register(definition)
        called = False

        def execute(_definition: ScheduleDefinition, _key: str) -> str:
            nonlocal called
            called = True
            return "should not execute"

        run = scheduler.trigger(definition.id, datetime(2030, 1, 1, 2, tzinfo=UTC), execute)
        self.assertEqual(run.status, ScheduleStatus.BLOCKED)
        self.assertFalse(called)

    def test_plugin_install_requires_review_and_records_integrity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = PluginRegistry(Path(directory) / "registry")
            source = ROOT / "plugins" / "erp-demo"
            with self.assertRaises(PluginInstallError):
                registry.install(source, approved_by="")
            record = registry.install(source, approved_by="test-maintainer")
            self.assertEqual(record.review_status, "approved:test-maintainer")
            self.assertEqual(len(registry.list_installed()), 1)
            manifest = json.loads(
                (
                    Path(directory) / "registry" / "erp-demo" / ".codex-plugin" / "plugin.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["name"], "erp-demo")

            upgraded = Path(directory) / "erp-demo-upgraded"
            shutil.copytree(source, upgraded)
            upgraded_manifest = upgraded / ".codex-plugin" / "plugin.json"
            upgraded_manifest.write_text(
                upgraded_manifest.read_text(encoding="utf-8").replace(
                    '"version": "0.1.0"', '"version": "0.1.1"'
                ),
                encoding="utf-8",
            )
            registry.install(upgraded, approved_by="test-maintainer")
            rolled_back = registry.rollback("erp-demo")
            self.assertEqual(rolled_back.version, "0.1.0")
            registry.remove("erp-demo", approved_by="test-maintainer")
            self.assertEqual(registry.list_installed(), [])
            events = (
                (Path(directory) / "registry" / "events.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            )
            self.assertEqual(
                [json.loads(event)["action"] for event in events],
                ["install", "install", "rollback", "remove"],
            )

    def test_http_api_requires_identity_and_preserves_idempotency(self) -> None:
        runtime, audit = build_runtime()
        client = TestClient(create_app(runtime, audit, auth_mode="headers"))
        response = client.post(
            "/api/v1/runs",
            headers={
                "X-User-Id": "demo-user",
                "X-Workspace-Id": "demo-workspace",
                "X-Tenant-Id": "demo-tenant",
                "X-Role": "customer",
                "Idempotency-Key": "test-order-status-1",
            },
            json={"query": "Where is my order?", "order_id": "SO-1001"},
        )
        self.assertEqual(response.status_code, 200)
        first = response.json()
        repeated = client.post(
            "/api/v1/runs",
            headers={
                "X-User-Id": "demo-user",
                "X-Workspace-Id": "demo-workspace",
                "X-Tenant-Id": "demo-tenant",
                "X-Role": "customer",
                "Idempotency-Key": "test-order-status-1",
            },
            json={"query": "Where is my order?", "order_id": "SO-1001"},
        )
        self.assertEqual(repeated.json()["run_id"], first["run_id"])
        events = client.get(
            f"/api/v1/runs/{first['run_id']}/events",
            headers={
                "X-User-Id": "demo-user",
                "X-Workspace-Id": "demo-workspace",
                "X-Tenant-Id": "demo-tenant",
                "X-Role": "customer",
            },
        )
        self.assertEqual(events.status_code, 200)
        self.assertEqual(
            {event["trace_id"] for event in events.json()["events"]}, {first["trace_id"]}
        )
        unauthorized = client.post("/api/v1/runs", json={"query": "Where?", "order_id": "SO-1001"})
        self.assertEqual(unauthorized.status_code, 401)
        wrong_workspace = client.get(
            f"/api/v1/runs/{first['run_id']}",
            headers={
                "X-User-Id": "demo-user",
                "X-Workspace-Id": "other-workspace",
                "X-Tenant-Id": "demo-tenant",
                "X-Role": "customer",
            },
        )
        self.assertEqual(wrong_workspace.status_code, 404)
        blocked = client.post(
            "/api/v1/runs",
            headers={
                "X-User-Id": "demo-user",
                "X-Workspace-Id": "demo-workspace",
                "X-Tenant-Id": "demo-tenant",
                "X-Role": "unknown-role",
            },
            json={"query": "Where is my order?", "order_id": "SO-1001"},
        )
        self.assertEqual(blocked.status_code, 403)

    def test_http_request_id_reaches_audit_and_metrics_are_auth_protected(self) -> None:
        runtime, audit = build_runtime()
        client = TestClient(create_app(runtime, audit, auth_mode="headers"))
        headers = {
            "X-User-Id": "demo-user",
            "X-Workspace-Id": "demo-workspace",
            "X-Tenant-Id": "demo-tenant",
            "X-Role": "customer",
            "X-Request-Id": "http-request-1",
        }
        created = client.post(
            "/api/v1/runs",
            headers=headers,
            json={"query": "Where is my order?", "order_id": "SO-1001"},
        )
        self.assertEqual(created.status_code, 200)
        self.assertEqual(created.headers["x-request-id"], "http-request-1")
        self.assertEqual({event.request_id for event in audit.events}, {"http-request-1"})
        metrics = client.get(
            "/metrics",
            headers={key: value for key, value in headers.items() if key != "X-Request-Id"},
        )
        self.assertEqual(metrics.status_code, 200)
        self.assertIn("http_requests_total", metrics.text)
        self.assertEqual(client.get("/metrics").status_code, 401)

    def test_http_api_bearer_mode_does_not_trust_identity_headers(self) -> None:
        runtime, audit = build_runtime()
        client = TestClient(
            create_app(runtime, audit, auth_mode="bearer", bearer_token="test-token")
        )
        missing = client.post(
            "/api/v1/runs",
            headers={
                "X-User-Id": "attacker",
                "X-Workspace-Id": "other-workspace",
                "X-Tenant-Id": "other-tenant",
                "X-Role": "admin",
            },
            json={"query": "Where is my order?", "order_id": "SO-1001"},
        )
        self.assertEqual(missing.status_code, 401)
        valid = client.post(
            "/api/v1/runs",
            headers={"Authorization": "Bearer test-token"},
            json={"query": "Where is my order?", "order_id": "SO-1001"},
        )
        self.assertEqual(valid.status_code, 200)

    def test_production_api_rejects_preview_auth_modes(self) -> None:
        runtime, audit = build_runtime()
        with self.assertRaises(ValueError):
            create_app(
                runtime,
                audit,
                auth_mode="bearer",
                bearer_token="preview-token",
                platform_env="production",
            )

    def test_jwt_authenticator_injects_identity_and_rejects_expired_tokens(self) -> None:
        import base64
        import hashlib
        import hmac
        import json
        import time

        secret = "s" * 32

        def segment(value: dict[str, object]) -> str:
            encoded = base64.urlsafe_b64encode(
                json.dumps(value, separators=(",", ":")).encode()
            ).decode()
            return encoded.rstrip("=")

        header = segment({"alg": "HS256", "typ": "JWT"})

        def token_for(expiry: float) -> str:
            claims = segment(
                {
                    "sub": "user-a",
                    "workspace_id": "workspace-a",
                    "tenant_id": "tenant-a",
                    "role": "customer",
                    "exp": expiry,
                }
            )
            signed = f"{header}.{claims}"
            signature = (
                base64.urlsafe_b64encode(
                    hmac.new(secret.encode(), signed.encode(), hashlib.sha256).digest()
                )
                .decode()
                .rstrip("=")
            )
            return f"Bearer {signed}.{signature}"

        token = token_for(time.time() + 60)
        identity = JwtHs256Authenticator(secret).authenticate(token)
        self.assertEqual(identity, IdentityContext("user-a", "workspace-a", "tenant-a", "customer"))
        with self.assertRaises(AuthenticationError):
            JwtHs256Authenticator(secret).authenticate(token_for(1))

    def test_oidc_jwks_authenticator_verifies_rs256_and_refreshes_unknown_kid(self) -> None:
        import base64
        import json
        import time

        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding, rsa

        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        public_numbers = private_key.public_key().public_numbers()

        def encoded(value: bytes) -> str:
            return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")

        jwks = {
            "keys": [
                {
                    "kty": "RSA",
                    "kid": "key-1",
                    "alg": "RS256",
                    "use": "sig",
                    "n": encoded(
                        public_numbers.n.to_bytes((public_numbers.n.bit_length() + 7) // 8, "big")
                    ),
                    "e": encoded(
                        public_numbers.e.to_bytes((public_numbers.e.bit_length() + 7) // 8, "big")
                    ),
                }
            ]
        }
        fetches = 0

        def fetch_jwks() -> dict[str, object]:
            nonlocal fetches
            fetches += 1
            return jwks

        authenticator = JwtJwksAuthenticator(
            "https://idp.example.test",
            audience="enterprise-copilot",
            allowed_hosts=["idp.example.test"],
            jwks_uri="https://idp.example.test/keys",
            jwks_fetcher=fetch_jwks,
        )
        header = encoded(
            json.dumps(
                {"alg": "RS256", "typ": "JWT", "kid": "key-1"}, separators=(",", ":")
            ).encode()
        )
        claims = encoded(
            json.dumps(
                {
                    "sub": "user-a",
                    "workspace_id": "workspace-a",
                    "tenant_id": "tenant-a",
                    "role": "customer",
                    "iss": "https://idp.example.test",
                    "aud": "enterprise-copilot",
                    "exp": time.time() + 60,
                },
                separators=(",", ":"),
            ).encode()
        )
        signed = f"{header}.{claims}".encode("ascii")
        signature = private_key.sign(signed, padding.PKCS1v15(), hashes.SHA256())
        token = f"Bearer {header}.{claims}.{encoded(signature)}"
        self.assertEqual(
            authenticator.authenticate(token),
            IdentityContext("user-a", "workspace-a", "tenant-a", "customer"),
        )
        self.assertEqual(fetches, 1)
        unknown_header = encoded(
            json.dumps(
                {"alg": "RS256", "typ": "JWT", "kid": "key-2"}, separators=(",", ":")
            ).encode()
        )
        unknown_kid = f"Bearer {unknown_header}.{claims}.{encoded(signature)}"
        with self.assertRaises(AuthenticationError):
            authenticator.authenticate(unknown_kid)
        self.assertEqual(fetches, 2)

    def test_redis_rate_limiter_uses_atomic_client_and_fails_closed(self) -> None:
        class FakeRedis:
            def __init__(self) -> None:
                self.calls: list[tuple[object, ...]] = []
                self.result: list[int] = [1, 0]

            def eval(self, *args: object) -> list[int]:
                self.calls.append(args)
                return self.result

        client = FakeRedis()
        limiter = RedisRateLimiter(client, 2, prefix="test")
        self.assertEqual(limiter.check("workspace:user"), (True, 0))
        self.assertEqual(client.calls[0][0], limiter._SCRIPT)
        client.result = [0, 4]
        self.assertEqual(limiter.check("workspace:user"), (False, 4))

        class BrokenRedis:
            def eval(self, *args: object) -> object:
                raise OSError("redis unavailable")

        self.assertEqual(RedisRateLimiter(BrokenRedis(), 1).check("key"), (False, 60))


if __name__ == "__main__":
    unittest.main()
