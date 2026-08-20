"""First vertical slice: Customer Service Agent order-status flow."""

from __future__ import annotations

import hashlib
import time
from uuid import uuid4

from packages.observability import TraceExporter, TraceRecord, new_span_id

from .audit import AuditLog
from .mcp import McpGateway
from .models import (
    AuditEvent,
    IdentityContext,
    PolicyDecision,
    RunResult,
    RunStatus,
    ToolCallRequest,
    ToolDefinition,
)
from .policy import PolicyEngine


class AgentRuntime:
    agent_id = "customer-service-agent"
    tool_name = "erp.get_order_status"

    def __init__(
        self,
        policy: PolicyEngine,
        gateway: McpGateway,
        audit: AuditLog,
        trace_exporter: TraceExporter | None = None,
    ) -> None:
        self._policy = policy
        self._gateway = gateway
        self._audit = audit
        self._trace_exporter = trace_exporter

    def gateway_health(self) -> dict[str, str]:
        """Expose transport health without exposing gateway credentials."""

        return self._gateway.health()

    def tool_definition(self, tool_name: str) -> ToolDefinition | None:
        """Expose only the registered typed definition to the API boundary."""

        return self._gateway.definitions.get(tool_name)

    def policy_decision(
        self,
        identity: IdentityContext,
        tool_name: str,
        *,
        approval_token: str | None = None,
    ) -> PolicyDecision:
        """Expose the same policy decision used before any tool call."""

        return self._policy.authorize(identity, tool_name, approval_token=approval_token)

    def run(
        self,
        query: str,
        identity: IdentityContext,
        *,
        order_id: str | None = None,
        request_id: str | None = None,
        trace_id: str | None = None,
    ) -> RunResult:
        request_id = request_id or f"req-{uuid4()}"
        trace_id = trace_id or f"trace-{uuid4()}"
        run_id = f"run-{uuid4()}"

        self._audit.append(
            AuditEvent(
                event_type="run.created",
                request_id=request_id,
                trace_id=trace_id,
                run_id=run_id,
                workspace_id=identity.workspace_id,
                agent_id=self.agent_id,
                payload={
                    "query_length": len(query) if isinstance(query, str) else 0,
                    "intent": "order_status",
                    "tenant_id": identity.tenant_id,
                },
            )
        )

        if not isinstance(query, str) or not query.strip():
            return self._finish(
                status=RunStatus.BLOCKED,
                message="A non-empty customer request is required.",
                request_id=request_id,
                trace_id=trace_id,
                run_id=run_id,
                identity=identity,
            )

        if not order_id or not order_id.strip():
            return self._finish(
                status=RunStatus.BLOCKED,
                message="Please provide an order ID so the authorized ERP record can be checked.",
                request_id=request_id,
                trace_id=trace_id,
                run_id=run_id,
                identity=identity,
            )

        decision = self._policy.authorize(identity, self.tool_name)
        self._audit.append(
            AuditEvent(
                event_type="policy.decided",
                request_id=request_id,
                trace_id=trace_id,
                run_id=run_id,
                workspace_id=identity.workspace_id,
                agent_id=self.agent_id,
                payload={
                    "tool": self.tool_name,
                    "outcome": decision.outcome,
                    "reason": decision.reason,
                    "risk": decision.risk.value if decision.risk else None,
                    "tenant_id": identity.tenant_id,
                },
            )
        )

        if decision.outcome != "allow":
            return self._finish(
                status=RunStatus.BLOCKED,
                message="This request is not authorized for the requested operation.",
                request_id=request_id,
                trace_id=trace_id,
                run_id=run_id,
                identity=identity,
            )

        tool_request = ToolCallRequest(
            request_id=request_id,
            trace_id=trace_id,
            run_id=run_id,
            identity=identity,
            tool_name=self.tool_name,
            arguments={"order_id": order_id.strip()},
            idempotency_key=f"{run_id}:{self.tool_name}:{order_id.strip()}",
        )
        tool_started = time.time_ns()
        tool_result = self._gateway.call(tool_request)
        self._export_tool_span(
            request_id=request_id,
            trace_id=trace_id,
            run_id=run_id,
            identity=identity,
            started_at=tool_started,
            success=tool_result.success,
        )
        self._audit.append(
            AuditEvent(
                event_type="tool.completed",
                request_id=request_id,
                trace_id=trace_id,
                run_id=run_id,
                workspace_id=identity.workspace_id,
                agent_id=self.agent_id,
                payload={
                    "tool": self.tool_name,
                    "success": tool_result.success,
                    "source_id": tool_result.source_id,
                    "external_ref": tool_result.external_ref,
                    "error": tool_result.error,
                    "tenant_id": identity.tenant_id,
                },
            )
        )

        if not tool_result.success:
            return self._finish(
                status=RunStatus.FAILED,
                message="The order status could not be verified at this time.",
                request_id=request_id,
                trace_id=trace_id,
                run_id=run_id,
                identity=identity,
            )

        if tool_result.external_ref != order_id.strip() or not tool_result.source_id:
            return self._finish(
                status=RunStatus.FAILED,
                message="The ERP result failed provenance verification.",
                request_id=request_id,
                trace_id=trace_id,
                run_id=run_id,
                identity=identity,
            )

        status = tool_result.data.get("status", "unknown")
        message = f"Order {order_id.strip()} is {status}. Observed at {tool_result.observed_at}."
        result = RunResult(
            status=RunStatus.SUCCEEDED,
            run_id=run_id,
            trace_id=trace_id,
            agent_id=self.agent_id,
            message=message,
            source_id=tool_result.source_id,
            observed_at=tool_result.observed_at,
            external_ref=tool_result.external_ref,
        )
        self._audit.append(
            AuditEvent(
                event_type="run.succeeded",
                request_id=request_id,
                trace_id=trace_id,
                run_id=run_id,
                workspace_id=identity.workspace_id,
                agent_id=self.agent_id,
                payload={
                    "source_id": result.source_id,
                    "external_ref": result.external_ref,
                    "tenant_id": identity.tenant_id,
                },
            )
        )
        return result

    def _export_tool_span(
        self,
        *,
        request_id: str,
        trace_id: str,
        run_id: str,
        identity: IdentityContext,
        started_at: int,
        success: bool,
    ) -> None:
        if self._trace_exporter is None:
            return
        try:
            self._trace_exporter.export(
                TraceRecord(
                    trace_id=trace_id,
                    span_id=new_span_id(),
                    name="agent.tool",
                    start_time_unix_nano=started_at,
                    end_time_unix_nano=time.time_ns(),
                    attributes={
                        "request_id": request_id,
                        "run_id": run_id,
                        "tool": self.tool_name,
                        "success": success,
                        "workspace_hash": hashlib.sha256(
                            identity.workspace_id.encode("utf-8")
                        ).hexdigest()[:16],
                    },
                )
            )
        except Exception as exc:  # noqa: BLE001 - trace failure is retained, not propagated
            self._audit.append(
                AuditEvent(
                    event_type="trace.export_failed",
                    request_id=request_id,
                    trace_id=trace_id,
                    run_id=run_id,
                    workspace_id=identity.workspace_id,
                    agent_id=self.agent_id,
                    payload={"error_type": type(exc).__name__, "tool": self.tool_name},
                )
            )

    def _finish(
        self,
        *,
        status: RunStatus,
        message: str,
        request_id: str,
        trace_id: str,
        run_id: str,
        identity: IdentityContext,
    ) -> RunResult:
        self._audit.append(
            AuditEvent(
                event_type=f"run.{status.value}",
                request_id=request_id,
                trace_id=trace_id,
                run_id=run_id,
                workspace_id=identity.workspace_id,
                agent_id=self.agent_id,
                payload={"message": message, "tenant_id": identity.tenant_id},
            )
        )
        return RunResult(
            status=status,
            run_id=run_id,
            trace_id=trace_id,
            agent_id=self.agent_id,
            message=message,
        )
