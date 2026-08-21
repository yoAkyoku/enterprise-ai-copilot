from __future__ import annotations

import unittest
from typing import ClassVar

from fastapi.testclient import TestClient

from packages.agent_runtime import (
    AgentRuntime,
    ApprovalService,
    AuditLog,
    IdentityContext,
    PolicyEngine,
    ToolCallRequest,
    ToolDefinition,
    ToolResult,
    ToolRisk,
    RunStatus,
)
from services.api.app import create_app


class CountingWriteGateway:
    server_id = "write-test"
    transport = "test"
    definitions: ClassVar[dict[str, ToolDefinition]] = {
        "erp.create_return": ToolDefinition(
            name="erp.create_return",
            risk=ToolRisk.WRITE,
            description="Create a return after approval.",
            allowed_roles=frozenset({"manager", "admin"}),
            argument_schema=(("order_id", "string"), ("reason", "string")),
        )
    }

    def __init__(self) -> None:
        self.calls: list[ToolCallRequest] = []

    def health(self) -> dict[str, str]:
        return {"server_id": self.server_id, "transport": self.transport, "status": "healthy"}

    def call(self, request: ToolCallRequest) -> ToolResult:
        self.calls.append(request)
        return ToolResult(
            success=True,
            data={
                "return_id": "RET-1001",
                "order_id": request.arguments["order_id"],
                "status": "requested",
            },
            source_id="erp:return:RET-1001",
            observed_at="2026-08-21T00:00:00+00:00",
            external_ref="RET-1001",
            workspace_id=request.identity.workspace_id,
            tenant_id=request.identity.tenant_id,
        )


def make_runtime() -> tuple[AgentRuntime, AuditLog, ApprovalService, CountingWriteGateway]:
    gateway = CountingWriteGateway()
    audit = AuditLog()
    approvals = ApprovalService()
    runtime = AgentRuntime(
        PolicyEngine(
            gateway.definitions,
            approval_verifier=lambda identity, approval_id, token, tool_name, arguments: (
                approvals.verify_and_consume(
                    identity,
                    approval_id,
                    token,
                    tool_name=tool_name,
                    arguments=arguments,
                )
            ),
        ),
        gateway,
        audit,
    )
    return runtime, audit, approvals, gateway


class ToolExecutionTests(unittest.TestCase):
    def test_generic_write_is_approval_bound_and_one_time(self) -> None:
        runtime, audit, approvals, gateway = make_runtime()
        requester = IdentityContext("manager-a", "workspace-a", "tenant-a", "manager")
        approver = IdentityContext("admin-a", "workspace-a", "tenant-a", "admin")
        arguments = {"order_id": "SO-1001", "reason": "damaged"}
        record = approvals.request(
            requester,
            tool_name="erp.create_return",
            arguments=arguments,
            risk=ToolRisk.WRITE,
            idempotency_key="return-1001",
        )
        _, token = approvals.approve(approver, record.id)

        blocked = runtime.execute_tool(
            requester,
            "erp.create_return",
            arguments,
            request_id="req-blocked",
            idempotency_key="return-1001",
        )
        self.assertEqual(blocked.status, RunStatus.BLOCKED)
        self.assertEqual(blocked.decision.outcome, "approval_required")
        self.assertEqual(gateway.calls, [])

        executed = runtime.execute_tool(
            requester,
            "erp.create_return",
            arguments,
            request_id="req-approved",
            approval_id=record.id,
            approval_token=token,
            idempotency_key="return-1001",
        )
        self.assertEqual(executed.status, RunStatus.SUCCEEDED)
        self.assertEqual(executed.result.external_ref, "RET-1001")
        self.assertEqual(len(gateway.calls), 1)

        replayed = runtime.execute_tool(
            requester,
            "erp.create_return",
            arguments,
            request_id="req-replay",
            approval_id=record.id,
            approval_token=token,
            idempotency_key="return-1001-replay",
        )
        self.assertEqual(replayed.status, RunStatus.BLOCKED)
        self.assertEqual(replayed.decision.outcome, "approval_required")
        self.assertEqual(len(gateway.calls), 1)
        self.assertIn("tool.completed", [event.event_type for event in audit.events])

        second = approvals.request(
            requester,
            tool_name="erp.create_return",
            arguments=arguments,
            risk=ToolRisk.WRITE,
            idempotency_key="return-1002",
        )
        _, second_token = approvals.approve(approver, second.id)
        invalid = runtime.execute_tool(
            requester,
            "erp.create_return",
            {"order_id": "SO-1001"},
            approval_id=second.id,
            approval_token=second_token,
            idempotency_key="return-1002",
        )
        self.assertEqual(invalid.status, RunStatus.BLOCKED)
        valid_after_invalid = runtime.execute_tool(
            requester,
            "erp.create_return",
            arguments,
            approval_id=second.id,
            approval_token=second_token,
            idempotency_key="return-1002",
        )
        self.assertEqual(valid_after_invalid.status, RunStatus.SUCCEEDED)
        self.assertEqual(len(gateway.calls), 2)

    def test_approval_token_cannot_be_used_by_another_user_in_same_tenant(self) -> None:
        runtime, _, approvals, gateway = make_runtime()
        requester = IdentityContext("manager-a", "workspace-a", "tenant-a", "manager")
        other_user = IdentityContext("manager-b", "workspace-a", "tenant-a", "manager")
        approver = IdentityContext("admin-a", "workspace-a", "tenant-a", "admin")
        arguments = {"order_id": "SO-1001", "reason": "damaged"}
        record = approvals.request(
            requester,
            tool_name="erp.create_return",
            arguments=arguments,
            risk=ToolRisk.WRITE,
        )
        _, token = approvals.approve(approver, record.id)
        result = runtime.execute_tool(
            other_user,
            "erp.create_return",
            arguments,
            approval_id=record.id,
            approval_token=token,
        )
        self.assertEqual(result.status, RunStatus.BLOCKED)
        self.assertEqual(gateway.calls, [])

    def test_cancellation_happens_before_approval_consumption(self) -> None:
        runtime, _, approvals, gateway = make_runtime()
        requester = IdentityContext("manager-a", "workspace-a", "tenant-a", "manager")
        approver = IdentityContext("admin-a", "workspace-a", "tenant-a", "admin")
        arguments = {"order_id": "SO-1001", "reason": "damaged"}
        record = approvals.request(
            requester,
            tool_name="erp.create_return",
            arguments=arguments,
            risk=ToolRisk.WRITE,
        )
        _, token = approvals.approve(approver, record.id)
        cancelled = runtime.execute_tool(
            requester,
            "erp.create_return",
            arguments,
            approval_id=record.id,
            approval_token=token,
            idempotency_key="cancelled-return-1001",
            cancel_checker=lambda: True,
        )
        self.assertEqual(cancelled.status, RunStatus.CANCELLED)
        self.assertEqual(gateway.calls, [])
        retry = runtime.execute_tool(
            requester,
            "erp.create_return",
            arguments,
            approval_id=record.id,
            approval_token=token,
            idempotency_key="cancelled-return-1001",
        )
        self.assertEqual(retry.status, RunStatus.SUCCEEDED)

    def test_http_generic_tool_execution_records_scoped_run(self) -> None:
        runtime, audit, approvals, gateway = make_runtime()
        client = TestClient(
            create_app(runtime, audit, auth_mode="headers", approval_service=approvals)
        )
        manager = {
            "X-User-Id": "manager-a",
            "X-Workspace-Id": "workspace-a",
            "X-Tenant-Id": "tenant-a",
            "X-Role": "manager",
        }
        requested = client.post(
            "/api/v1/approvals",
            headers=manager,
            json={
                "tool_name": "erp.create_return",
                "arguments": {"order_id": "SO-1001", "reason": "damaged"},
                "idempotency_key": "http-return-1001",
            },
        )
        self.assertEqual(requested.status_code, 201, requested.text)
        approval_id = requested.json()["id"]
        admin = {**manager, "X-User-Id": "admin-a", "X-Role": "admin"}
        approved = client.post(f"/api/v1/approvals/{approval_id}/approve", headers=admin)
        self.assertEqual(approved.status_code, 200, approved.text)
        token = approved.json()["approval_token"]

        needs_approval = client.post(
            "/api/v1/tools/erp.create_return/execute",
            headers={**manager, "Idempotency-Key": "http-tool-1001"},
            json={"arguments": {"order_id": "SO-1001", "reason": "damaged"}},
        )
        self.assertEqual(needs_approval.status_code, 409, needs_approval.text)

        executed = client.post(
            "/api/v1/tools/erp.create_return/execute",
            headers={**manager, "Idempotency-Key": "http-tool-1001"},
            json={
                "arguments": {"order_id": "SO-1001", "reason": "damaged"},
                "approval_id": approval_id,
                "approval_token": token,
            },
        )
        self.assertEqual(executed.status_code, 200, executed.text)
        body = executed.json()
        self.assertEqual(body["status"], "succeeded")
        self.assertEqual(body["data"]["return_id"], "RET-1001")
        self.assertEqual(len(gateway.calls), 1)

        events = client.get(f"/api/v1/runs/{body['run_id']}/events", headers=manager)
        self.assertEqual(events.status_code, 200, events.text)
        self.assertIn("tool.completed", [event["event_type"] for event in events.json()["events"]])

        replay = client.post(
            "/api/v1/tools/erp.create_return/execute",
            headers={**manager, "Idempotency-Key": "http-tool-replay"},
            json={
                "arguments": {"order_id": "SO-1001", "reason": "damaged"},
                "approval_id": approval_id,
                "approval_token": token,
            },
        )
        self.assertEqual(replay.status_code, 409, replay.text)
        self.assertEqual(len(gateway.calls), 1)


if __name__ == "__main__":
    unittest.main()
