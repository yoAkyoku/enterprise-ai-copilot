from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.api_acceptance_smoke import Principal, Response, load_configuration, run_acceptance


class _FakeAcceptanceClient:
    def __init__(self) -> None:
        self.attachments: dict[str, set[str]] = {"token-a": set(), "token-b": set()}

    @staticmethod
    def multipart_image() -> tuple[bytes, str]:
        return b"synthetic", "image/png"

    def request(
        self,
        method: str,
        path: str,
        *,
        token: str | None = None,
        body: bytes | None = None,
        content_type: str | None = None,
        idempotency_key: str | None = None,
    ) -> Response:
        del body, content_type, idempotency_key
        if path == "/health":
            return Response(200, b'{"status":"ok"}')
        if path == "/ready":
            return Response(200, b'{"status":"ready"}')
        if path == "/api/v1/dashboard" and token in self.attachments:
            suffix = token[-1]
            payload = {"workspace_id": f"workspace-{suffix}", "tenant_id": f"tenant-{suffix}"}
            return Response(200, json.dumps(payload).encode())
        if path == "/api/v1/runs" and method == "POST":
            return Response(200, b'{"status":"succeeded","run_id":"run-1"}')
        if path == "/api/v1/runs/run-1":
            return Response(200 if token == "token-a" else 404, b"{}")
        if path == "/api/v1/attachments" and method == "POST" and token in self.attachments:
            attachment_id = f"attachment-{token[-1]}"
            self.attachments[token].add(attachment_id)
            return Response(201, json.dumps({"id": attachment_id}).encode())
        if path == "/api/v1/attachments" and method == "GET" and token in self.attachments:
            return Response(
                200,
                json.dumps({"items": [{"id": item} for item in self.attachments[token]]}).encode(),
            )
        for principal, own_ids in self.attachments.items():
            for attachment_id in list(own_ids):
                if path == f"/api/v1/attachments/{attachment_id}":
                    if method == "DELETE":
                        own_ids.remove(attachment_id)
                        return Response(204, b"")
                    return Response(200 if token == principal else 404, b"{}")
                if path == f"/api/v1/attachments/{attachment_id}/content":
                    return Response(200 if token == principal else 404, b"image")
        return Response(404, b"")


class ApiAcceptanceConfigurationTests(unittest.TestCase):
    def test_run_acceptance_checks_scope_and_cleans_fixtures(self) -> None:
        client = _FakeAcceptanceClient()
        principal_a = Principal("principal-a", "token-a", "workspace-a", "tenant-a")
        principal_b = Principal("principal-b", "token-b", "workspace-b", "tenant-b")

        result = run_acceptance(client, principal_a, principal_b, "SMOKE-0001")

        self.assertEqual(result["status"], "PASS")
        self.assertIn({"name": "attachment.tenant_isolation", "status": "PASS"}, result["checks"])
        self.assertEqual(client.attachments, {"token-a": set(), "token-b": set()})

    def test_connector_smoke_module_imports_without_network(self) -> None:
        from scripts import connector_smoke

        self.assertTrue(callable(connector_smoke.main))

    def test_connector_smoke_accepts_reviewed_order_fixture(self) -> None:
        from scripts.connector_smoke import _smoke_order_id

        with patch.dict(os.environ, {"AGENT_ACCEPTANCE_ORDER_ID": "ORDER-42"}, clear=False):
            self.assertEqual(_smoke_order_id(), "ORDER-42")

    def test_configuration_requires_two_distinct_scopes_and_tokens(self) -> None:
        values = {
            "AGENT_ACCEPTANCE_BASE_URL": "https://api.example.test",
            "AGENT_ACCEPTANCE_ALLOWED_HOSTS": "api.example.test",
            "AGENT_ACCEPTANCE_A_BEARER_TOKEN": "token-a",
            "AGENT_ACCEPTANCE_A_WORKSPACE_ID": "workspace-a",
            "AGENT_ACCEPTANCE_A_TENANT_ID": "tenant-a",
            "AGENT_ACCEPTANCE_B_BEARER_TOKEN": "token-b",
            "AGENT_ACCEPTANCE_B_WORKSPACE_ID": "workspace-b",
            "AGENT_ACCEPTANCE_B_TENANT_ID": "tenant-b",
            "AGENT_ACCEPTANCE_ORDER_ID": "SMOKE-0001",
        }
        with patch.dict(os.environ, values, clear=False):
            client, principal_a, principal_b, order_id = load_configuration()
        self.assertEqual(client.base_url, "https://api.example.test")
        self.assertEqual(principal_a.tenant_id, "tenant-a")
        self.assertEqual(principal_b.workspace_id, "workspace-b")
        self.assertEqual(order_id, "SMOKE-0001")

    def test_configuration_rejects_same_scope(self) -> None:
        values = {
            "AGENT_ACCEPTANCE_BASE_URL": "https://api.example.test",
            "AGENT_ACCEPTANCE_ALLOWED_HOSTS": "api.example.test",
            "AGENT_ACCEPTANCE_A_BEARER_TOKEN": "token-a",
            "AGENT_ACCEPTANCE_A_WORKSPACE_ID": "workspace",
            "AGENT_ACCEPTANCE_A_TENANT_ID": "tenant",
            "AGENT_ACCEPTANCE_B_BEARER_TOKEN": "token-b",
            "AGENT_ACCEPTANCE_B_WORKSPACE_ID": "workspace",
            "AGENT_ACCEPTANCE_B_TENANT_ID": "tenant",
            "AGENT_ACCEPTANCE_ORDER_ID": "SMOKE-0001",
        }
        with patch.dict(os.environ, values, clear=False):
            with self.assertRaisesRegex(ValueError, "different scopes"):
                load_configuration()

    def test_configuration_rejects_non_https_acceptance_endpoint(self) -> None:
        values = {
            "AGENT_ACCEPTANCE_BASE_URL": "http://api.example.test",
            "AGENT_ACCEPTANCE_ALLOWED_HOSTS": "api.example.test",
            "AGENT_ACCEPTANCE_A_BEARER_TOKEN": "token-a",
            "AGENT_ACCEPTANCE_A_WORKSPACE_ID": "workspace-a",
            "AGENT_ACCEPTANCE_A_TENANT_ID": "tenant-a",
            "AGENT_ACCEPTANCE_B_BEARER_TOKEN": "token-b",
            "AGENT_ACCEPTANCE_B_WORKSPACE_ID": "workspace-b",
            "AGENT_ACCEPTANCE_B_TENANT_ID": "tenant-b",
            "AGENT_ACCEPTANCE_ORDER_ID": "SMOKE-0001",
        }
        with patch.dict(os.environ, values, clear=False):
            with self.assertRaisesRegex(ValueError, "HTTPS"):
                load_configuration()

    def test_production_acceptance_workflow_is_manual_and_protected(self) -> None:
        root = Path(__file__).resolve().parents[1]
        workflow = (root / ".github" / "workflows" / "production-acceptance.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("workflow_dispatch:", workflow)
        self.assertIn("environment: ${{ inputs.target_environment }}", workflow)
        self.assertIn("--confirm-live", workflow)
        self.assertIn("if: always()", workflow)
        self.assertNotIn("\n  push:", workflow)
