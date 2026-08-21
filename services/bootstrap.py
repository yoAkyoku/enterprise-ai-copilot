"""Safe synthetic dependency wiring for local CLI, API and tests."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path

from packages.agent_runtime import (
    AgentRuntime,
    AuditLog,
    IdentityContext,
    InMemoryMcpGateway,
    ApprovalService,
    McpGateway,
    ModelProvider,
    OpenAICompatibleModelProvider,
    PolicyEngine,
    StreamableHttpMcpGateway,
    ToolDefinition,
    ToolRisk,
)
from packages.observability import OtlpHttpTraceExporter, TraceExporter


def build_model_provider(platform_env: str | None = None) -> ModelProvider | None:
    """Build the reviewed, consent-gated model adapter from deployment config."""

    resolved_env = (platform_env or os.getenv("AGENT_PLATFORM_ENV", "development")).lower()
    endpoint = os.getenv("AGENT_MODEL_ENDPOINT", "").strip()
    api_key = os.getenv("AGENT_MODEL_API_KEY", "").strip()
    model = os.getenv("AGENT_MODEL_NAME", "").strip()
    values = (endpoint, api_key, model)
    if not any(values):
        if resolved_env in {"staging", "production"}:
            raise RuntimeError(
                "staging and production require AGENT_MODEL_ENDPOINT, "
                "AGENT_MODEL_API_KEY and AGENT_MODEL_NAME"
            )
        return None
    if not all(values):
        raise RuntimeError(
            "AGENT_MODEL_ENDPOINT, AGENT_MODEL_API_KEY and AGENT_MODEL_NAME "
            "must be configured together"
        )
    try:
        timeout_seconds = float(os.getenv("AGENT_MODEL_TIMEOUT_SECONDS", "30"))
        max_output_chars = int(os.getenv("AGENT_MODEL_MAX_OUTPUT_CHARS", "4000"))
    except ValueError as exc:
        raise RuntimeError("model timeout and output limit must be numeric") from exc
    allowed_hosts = [
        item.strip()
        for item in os.getenv("AGENT_MODEL_ALLOWED_HOSTS", "").split(",")
        if item.strip()
    ]
    return OpenAICompatibleModelProvider(
        endpoint,
        api_key,
        model,
        allowed_hosts=allowed_hosts,
        timeout_seconds=timeout_seconds,
        max_output_chars=max_output_chars,
    )


def build_trace_exporter(platform_env: str | None = None) -> TraceExporter | None:
    """Build the configured OTLP exporter and fail closed in production."""

    resolved_env = (platform_env or os.getenv("AGENT_PLATFORM_ENV", "development")).lower()
    endpoint = os.getenv("AGENT_TRACE_ENDPOINT", "").strip()
    if not endpoint:
        if resolved_env in {"staging", "production"}:
            raise RuntimeError("staging and production require AGENT_TRACE_ENDPOINT")
        return None
    return OtlpHttpTraceExporter(
        endpoint,
        allowed_hosts=os.getenv("AGENT_TRACE_ALLOWED_HOSTS", "").split(","),
        bearer_token=os.getenv("AGENT_TRACE_BEARER_TOKEN") or None,
        timeout_seconds=float(os.getenv("AGENT_TRACE_TIMEOUT_SECONDS", "5")),
    )


def build_erp_tool_definitions() -> dict[str, ToolDefinition]:
    """Return the reviewed ERP contract shared by local and remote gateways."""

    return {
        "erp.get_order_status": ToolDefinition(
            name="erp.get_order_status",
            risk=ToolRisk.READ,
            description="Return a tenant-scoped ERP order status with provenance.",
        )
    }


def build_runtime(
    audit: AuditLog | None = None,
    trace_exporter: TraceExporter | None = None,
    model_provider: ModelProvider | None = None,
    approval_service: ApprovalService | None = None,
) -> tuple[AgentRuntime, AuditLog]:
    seed_path = Path(__file__).resolve().parents[1] / "data" / "demo" / "orders.json"
    if seed_path.is_file():
        rows = json.loads(seed_path.read_text(encoding="utf-8"))
        orders = {(row["tenant_id"], row["order_id"]): row for row in rows}
    else:
        orders = {
            ("demo-tenant", "SO-1001"): {
                "order_id": "SO-1001",
                "status": "in_transit",
                "customer_id": "CUS-001",
                "updated_at": "2026-08-19T08:00:00+08:00",
            }
        }
    gateway: McpGateway = InMemoryMcpGateway(orders)
    if os.getenv("AGENT_PROVIDER_MODE", "synthetic").lower() == "remote":
        endpoint = os.getenv("AGENT_MCP_ENDPOINT", "").strip()
        if not endpoint:
            raise RuntimeError("AGENT_MCP_ENDPOINT is required when AGENT_PROVIDER_MODE=remote")
        gateway = StreamableHttpMcpGateway(
            endpoint,
            build_erp_tool_definitions(),
            allowed_hosts=os.getenv("AGENT_MCP_ALLOWED_HOSTS", "").split(","),
            bearer_token=os.getenv("AGENT_MCP_BEARER_TOKEN") or None,
            timeout_seconds=float(os.getenv("AGENT_MCP_TIMEOUT_SECONDS", "10")),
        )
    audit_log = audit or AuditLog()
    configured_model = model_provider
    if configured_model is None:
        configured_model = build_model_provider()
    approval_verifier = None
    if approval_service is not None:

        def verify_approval(
            identity: IdentityContext,
            approval_id: str,
            token: str,
            tool_name: str,
            arguments: Mapping[str, object],
        ) -> bool:
            return approval_service.verify_and_consume(
                identity,
                approval_id,
                token,
                tool_name=tool_name,
                arguments=arguments,
            )

        approval_verifier = verify_approval
    return (
        AgentRuntime(
            PolicyEngine(gateway.definitions, approval_verifier=approval_verifier),
            gateway,
            audit_log,
            trace_exporter=trace_exporter,
            model_provider=configured_model,
        ),
        audit_log,
    )


def demo_identity() -> IdentityContext:
    return IdentityContext(
        user_id="demo-user",
        workspace_id="demo-workspace",
        tenant_id="demo-tenant",
        role="customer",
    )
