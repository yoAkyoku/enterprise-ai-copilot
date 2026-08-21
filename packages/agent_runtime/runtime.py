"""First vertical slice: Customer Service Agent order-status flow."""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from uuid import uuid4

from packages.observability import TraceExporter, TraceRecord, new_span_id

from .audit import AuditLog
from .mcp import McpGateway
from .model import ModelCompletion, ModelProvider, ModelProviderError
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

_MAX_STATUS_LENGTH = 256


class AgentRuntime:
    agent_id = "customer-service-agent"
    tool_name = "erp.get_order_status"

    def __init__(
        self,
        policy: PolicyEngine,
        gateway: McpGateway,
        audit: AuditLog,
        trace_exporter: TraceExporter | None = None,
        model_provider: ModelProvider | None = None,
    ) -> None:
        self._policy = policy
        self._gateway = gateway
        self._audit = audit
        self._trace_exporter = trace_exporter
        self._model_provider = model_provider

    def gateway_health(self) -> dict[str, str]:
        """Expose transport health without exposing gateway credentials."""

        return self._gateway.health()

    def model_health(self) -> dict[str, str]:
        """Expose model configuration health without making a provider call."""

        if self._model_provider is None:
            return {"provider": "none", "model": "none", "status": "disabled"}
        return self._model_provider.health()

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
        allow_external_model_processing: bool = False,
        cancel_checker: Callable[[], bool] | None = None,
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

        if cancel_checker is not None and cancel_checker():
            return self._finish(
                status=RunStatus.CANCELLED,
                message="This run was cancelled before the ERP tool call.",
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
        requested_order_id = order_id.strip()
        result_data = tool_result.data if isinstance(tool_result.data, dict) else {}
        returned_order_id = result_data.get("order_id")
        returned_tenant_id = result_data.get("tenant_id")
        returned_status = result_data.get("status")
        provenance_verified = (
            tool_result.success
            and tool_result.workspace_id == identity.workspace_id
            and tool_result.tenant_id == identity.tenant_id
            and tool_result.external_ref == requested_order_id
            and isinstance(tool_result.source_id, str)
            and bool(tool_result.source_id.strip())
            and isinstance(tool_result.observed_at, str)
            and bool(tool_result.observed_at.strip())
            and returned_order_id == requested_order_id
            and isinstance(returned_status, str)
            and bool(returned_status.strip())
            and len(returned_status) <= _MAX_STATUS_LENGTH
            and (returned_tenant_id is None or returned_tenant_id == identity.tenant_id)
        )
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
                    "provenance_verified": provenance_verified,
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

        if not provenance_verified:
            return self._finish(
                status=RunStatus.FAILED,
                message="The ERP result failed provenance verification.",
                request_id=request_id,
                trace_id=trace_id,
                run_id=run_id,
                identity=identity,
            )

        if cancel_checker is not None and cancel_checker():
            return self._finish(
                status=RunStatus.CANCELLED,
                message="This run was cancelled before the next external operation.",
                request_id=request_id,
                trace_id=trace_id,
                run_id=run_id,
                identity=identity,
                source_id=tool_result.source_id,
                observed_at=tool_result.observed_at,
                external_ref=tool_result.external_ref,
            )

        status = returned_status
        verified_message = (
            f"Order {requested_order_id} is {status}. Observed at {tool_result.observed_at}."
        )
        message = verified_message
        result_status = RunStatus.SUCCEEDED
        if self._model_provider is not None:
            if not allow_external_model_processing:
                self._audit.append(
                    AuditEvent(
                        event_type="model.blocked",
                        request_id=request_id,
                        trace_id=trace_id,
                        run_id=run_id,
                        workspace_id=identity.workspace_id,
                        agent_id=self.agent_id,
                        payload={
                            "provider": self._model_provider.provider_id,
                            "reason": "external_processing_consent_required",
                            "external_processing_consent": False,
                            "tenant_id": identity.tenant_id,
                        },
                    )
                )
                message = f"{verified_message} Model explanation was not requested."
            else:
                model_started = time.time_ns()
                try:
                    completion = self._model_provider.complete(
                        query,
                        {
                            "order_id": requested_order_id,
                            "status": str(status),
                            "source_id": tool_result.source_id,
                            "observed_at": tool_result.observed_at or "",
                        },
                        request_id=request_id,
                        trace_id=trace_id,
                        run_id=run_id,
                    )
                    if (
                        not isinstance(completion, ModelCompletion)
                        or not isinstance(completion.provider, str)
                        or not completion.provider.strip()
                        or not isinstance(completion.model, str)
                        or not completion.model.strip()
                        or not isinstance(completion.text, str)
                        or not completion.text.strip()
                        or len(completion.text) > 32_000
                    ):
                        raise ModelProviderError("model provider returned an invalid completion")
                except ModelProviderError as exc:
                    result_status = RunStatus.PARTIAL_SUCCESS
                    self._export_model_span(
                        request_id=request_id,
                        trace_id=trace_id,
                        run_id=run_id,
                        identity=identity,
                        started_at=model_started,
                        provider=self._model_provider.provider_id,
                        model=self._model_provider.model,
                        success=False,
                    )
                    self._audit.append(
                        AuditEvent(
                            event_type="model.failed",
                            request_id=request_id,
                            trace_id=trace_id,
                            run_id=run_id,
                            workspace_id=identity.workspace_id,
                            agent_id=self.agent_id,
                            payload={
                                "provider": self._model_provider.provider_id,
                                "error_type": type(exc).__name__,
                                "tenant_id": identity.tenant_id,
                            },
                        )
                    )
                    message = f"{verified_message} Model explanation is unavailable."
                else:
                    self._audit.append(
                        AuditEvent(
                            event_type="model.completed",
                            request_id=request_id,
                            trace_id=trace_id,
                            run_id=run_id,
                            workspace_id=identity.workspace_id,
                            agent_id=self.agent_id,
                            payload={
                                "provider": completion.provider,
                                "model": completion.model,
                                "external_processing_consent": True,
                                "tenant_id": identity.tenant_id,
                            },
                        )
                    )
                    self._export_model_span(
                        request_id=request_id,
                        trace_id=trace_id,
                        run_id=run_id,
                        identity=identity,
                        started_at=model_started,
                        provider=completion.provider,
                        model=completion.model,
                        success=True,
                    )
                    message = (
                        f"{verified_message}\n\nModel explanation (unverified): {completion.text}"
                    )
        result = RunResult(
            status=result_status,
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
                event_type=f"run.{result.status.value}",
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

    def _export_model_span(
        self,
        *,
        request_id: str,
        trace_id: str,
        run_id: str,
        identity: IdentityContext,
        started_at: int,
        provider: str,
        model: str,
        success: bool,
    ) -> None:
        if self._trace_exporter is None:
            return
        try:
            self._trace_exporter.export(
                TraceRecord(
                    trace_id=trace_id,
                    span_id=new_span_id(),
                    name="agent.model",
                    start_time_unix_nano=started_at,
                    end_time_unix_nano=time.time_ns(),
                    attributes={
                        "request_id": request_id,
                        "run_id": run_id,
                        "provider": provider,
                        "model": model,
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
                    payload={
                        "error_type": type(exc).__name__,
                        "tenant_id": identity.tenant_id,
                        "tool": "model",
                    },
                )
            )

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
                    payload={
                        "error_type": type(exc).__name__,
                        "tenant_id": identity.tenant_id,
                        "tool": self.tool_name,
                    },
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
        source_id: str | None = None,
        observed_at: str | None = None,
        external_ref: str | None = None,
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
            source_id=source_id,
            observed_at=observed_at,
            external_ref=external_ref,
        )
