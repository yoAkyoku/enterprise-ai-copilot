from __future__ import annotations

import unittest
from pathlib import Path

from packages.agent_runtime import (
    AgentRuntime,
    AuditLog,
    IdentityContext,
    InMemoryMcpGateway,
    PolicyEngine,
    RunStatus,
)

ROOT = Path(__file__).resolve().parents[1]


def make_runtime() -> tuple[AgentRuntime, AuditLog]:
    gateway = InMemoryMcpGateway(
        {
            ("tenant-a", "SO-1001"): {
                "order_id": "SO-1001",
                "status": "in_transit",
                "customer_id": "CUS-001",
            },
            ("tenant-b", "SO-1001"): {
                "order_id": "SO-1001",
                "status": "delivered",
                "customer_id": "CUS-999",
            },
        }
    )
    audit = AuditLog()
    return AgentRuntime(PolicyEngine(gateway.definitions), gateway, audit), audit


class VerticalSliceTests(unittest.TestCase):
    def test_customer_order_status_returns_verified_evidence(self) -> None:
        runtime, audit = make_runtime()
        result = runtime.run(
            "Where is my order?",
            IdentityContext("user-a", "workspace-a", "tenant-a", "customer"),
            order_id="SO-1001",
            request_id="req-test-success",
        )

        self.assertEqual(result.status, RunStatus.SUCCEEDED)
        self.assertIn("in_transit", result.message)
        self.assertEqual(result.external_ref, "SO-1001")
        self.assertTrue(result.source_id)
        self.assertEqual(audit.events[-1].event_type, "run.succeeded")
        self.assertEqual({event.trace_id for event in audit.events}, {result.trace_id})

    def test_missing_order_id_blocks_without_tool_call(self) -> None:
        runtime, audit = make_runtime()
        result = runtime.run(
            "Where is my order?",
            IdentityContext("user-a", "workspace-a", "tenant-a", "customer"),
            request_id="req-test-missing-order",
        )

        self.assertEqual(result.status, RunStatus.BLOCKED)
        self.assertIn("order ID", result.message)
        self.assertFalse(any(event.event_type == "tool.completed" for event in audit.events))

    def test_role_without_tool_permission_is_blocked(self) -> None:
        runtime, audit = make_runtime()
        result = runtime.run(
            "Where is my order?",
            IdentityContext("user-a", "workspace-a", "tenant-a", "unknown-role"),
            order_id="SO-1001",
        )

        self.assertEqual(result.status, RunStatus.BLOCKED)
        self.assertIn("not authorized", result.message)
        self.assertFalse(any(event.event_type == "tool.completed" for event in audit.events))

    def test_order_lookup_cannot_cross_tenant_scope(self) -> None:
        runtime, _ = make_runtime()
        result = runtime.run(
            "Where is my order?",
            IdentityContext("user-a", "workspace-a", "tenant-a", "customer"),
            order_id="SO-9999",
        )

        self.assertEqual(result.status, RunStatus.FAILED)
        self.assertIn("could not be verified", result.message)

    def test_identity_is_required(self) -> None:
        runtime, audit = make_runtime()
        result = runtime.run(
            "Where is my order?",
            IdentityContext("", "workspace-a", "tenant-a", "customer"),
            order_id="SO-1001",
        )

        self.assertEqual(result.status, RunStatus.BLOCKED)
        self.assertFalse(any(event.event_type == "tool.completed" for event in audit.events))

    def test_repository_contract_files_exist(self) -> None:
        required = (
            "AGENTS.md",
            "docs/SDD.md",
            "docs/VALIDATION_STANDARD.md",
            "agents/customer-service/AGENT.md",
            "agents/customer-service/agent.yaml",
            ".agents/skills/order-status/SKILL.md",
            "mcp/servers.yaml",
            ".env.example",
            "LICENSE",
            "docker-compose.yml",
            "infra/migrations/001_run_events.sql",
            "infra/migrations/002_attachments.sql",
            "infra/migrations/003_agent_runs.sql",
            "infra/migrations/004_approval_requests.sql",
            "apps/web/index.html",
            "data/demo/orders.json",
            "MANIFEST.in",
            "scripts/production_preflight.py",
            "scripts/connector_smoke.py",
        )
        for relative_path in required:
            with self.subTest(relative_path=relative_path):
                self.assertTrue((ROOT / relative_path).is_file())


if __name__ == "__main__":
    unittest.main()
