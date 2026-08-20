from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar

from fastapi.testclient import TestClient

from packages.agent_runtime import (
    AgentRuntime,
    ApprovalError,
    ApprovalService,
    AuditLog,
    IdentityContext,
    PolicyEngine,
    SQLiteApprovalStore,
    ToolDefinition,
    ToolRisk,
)
from services.api.app import create_app


class WriteGateway:
    server_id = "write-test"
    transport = "test"
    definitions: ClassVar[dict[str, ToolDefinition]] = {
        "erp.create_return": ToolDefinition(
            name="erp.create_return",
            risk=ToolRisk.WRITE,
            description="Create a return after approval.",
        )
    }

    def health(self) -> dict[str, str]:
        return {"server_id": self.server_id, "transport": self.transport, "status": "healthy"}

    def call(self, request: object) -> object:
        raise AssertionError("write gateway must not be called by approval tests")


class ApprovalServiceTests(unittest.TestCase):
    def test_approval_is_scoped_expiring_and_argument_bound(self) -> None:
        service = ApprovalService()
        requester = IdentityContext("user-a", "workspace-a", "tenant-a", "customer")
        approver = IdentityContext("manager-a", "workspace-a", "tenant-a", "manager")
        record = service.request(
            requester,
            tool_name="erp.create_return",
            arguments={"order_id": "SO-1001", "reason": "damaged"},
            risk=ToolRisk.WRITE,
            idempotency_key="approval-1",
        )
        repeated = service.request(
            requester,
            tool_name="erp.create_return",
            arguments={"order_id": "SO-1001", "reason": "different"},
            risk=ToolRisk.WRITE,
            idempotency_key="approval-1",
        )
        self.assertEqual(repeated.id, record.id)
        with self.assertRaises(ApprovalError):
            service.approve(
                IdentityContext("user-b", "workspace-a", "tenant-a", "customer"), record.id
            )
        approved, token = service.approve(approver, record.id)
        self.assertEqual(approved.status, "approved")
        self.assertNotIn(token, approved.as_dict().__repr__())
        self.assertTrue(
            service.verify(
                requester,
                record.id,
                token,
                tool_name="erp.create_return",
                arguments={"order_id": "SO-1001", "reason": "damaged"},
            )
        )
        self.assertFalse(
            service.verify(
                requester,
                record.id,
                token,
                tool_name="erp.create_return",
                arguments={"order_id": "SO-1001", "reason": "tampered"},
            )
        )

    def test_sqlite_approval_persists_without_raw_token(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "platform.sqlite3"
            first_store = SQLiteApprovalStore(path)
            first = ApprovalService(first_store)
            requester = IdentityContext("user-a", "workspace-a", "tenant-a", "customer")
            approver = IdentityContext("admin-a", "workspace-a", "tenant-a", "admin")
            record = first.request(
                requester,
                tool_name="erp.refund",
                arguments={"order_id": "SO-1001", "amount": 10},
                risk=ToolRisk.DESTRUCTIVE,
            )
            _, token = first.approve(approver, record.id)
            first.close()

            connection = sqlite3.connect(path)
            row = connection.execute(
                "SELECT token_hash FROM approval_requests WHERE id = ?", (record.id,)
            ).fetchone()
            connection.close()
            self.assertIsNotNone(row[0])
            self.assertNotEqual(row[0], token)

            reopened = ApprovalService(SQLiteApprovalStore(path))
            try:
                self.assertTrue(
                    reopened.verify(
                        requester,
                        record.id,
                        token,
                        tool_name="erp.refund",
                        arguments={"order_id": "SO-1001", "amount": 10},
                    )
                )
            finally:
                reopened.close()

    def test_http_approval_queue_requires_authorized_approver(self) -> None:
        audit = AuditLog()
        gateway = WriteGateway()
        runtime = AgentRuntime(
            PolicyEngine(
                gateway.definitions,
                role_allowlist={
                    "customer": {"erp.create_return"},
                    "manager": {"erp.create_return"},
                },
            ),
            gateway,
            audit,
        )
        approvals = ApprovalService()
        client = TestClient(
            create_app(runtime, audit, auth_mode="headers", approval_service=approvals)
        )
        requester = {
            "X-User-Id": "user-a",
            "X-Workspace-Id": "workspace-a",
            "X-Tenant-Id": "tenant-a",
            "X-Role": "customer",
        }
        created = client.post(
            "/api/v1/approvals",
            headers={**requester, "X-Request-Id": "approval-http-1"},
            json={"tool_name": "erp.create_return", "arguments": {"order_id": "SO-1001"}},
        )
        self.assertEqual(created.status_code, 201, created.text)
        approval_id = created.json()["id"]
        listed = client.get("/api/v1/approvals?approval_status=pending", headers=requester)
        self.assertEqual(listed.json()["count"], 1)
        denied = client.post(f"/api/v1/approvals/{approval_id}/approve", headers=requester)
        self.assertEqual(denied.status_code, 403)
        manager = {**requester, "X-User-Id": "manager-a", "X-Role": "manager"}
        approved = client.post(f"/api/v1/approvals/{approval_id}/approve", headers=manager)
        self.assertEqual(approved.status_code, 200, approved.text)
        self.assertTrue(approved.json()["approval_token"])
        self.assertEqual(approved.headers["cache-control"], "private, no-store")
        self.assertIn("approval.requested", [event.event_type for event in audit.events])


if __name__ == "__main__":
    unittest.main()
