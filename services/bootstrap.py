"""Safe synthetic dependency wiring for local CLI, API and tests."""

from __future__ import annotations

import json
import os
from pathlib import Path

from packages.agent_runtime import (
    AgentRuntime,
    AuditLog,
    IdentityContext,
    InMemoryMcpGateway,
    McpGateway,
    PolicyEngine,
    StreamableHttpMcpGateway,
)
from packages.observability import OtlpHttpTraceExporter, TraceExporter


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


def build_runtime(
    audit: AuditLog | None = None,
    trace_exporter: TraceExporter | None = None,
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
        synthetic_definitions = InMemoryMcpGateway({}).definitions
        gateway = StreamableHttpMcpGateway(
            endpoint,
            synthetic_definitions,
            allowed_hosts=os.getenv("AGENT_MCP_ALLOWED_HOSTS", "").split(","),
            bearer_token=os.getenv("AGENT_MCP_BEARER_TOKEN") or None,
            timeout_seconds=float(os.getenv("AGENT_MCP_TIMEOUT_SECONDS", "10")),
        )
    audit_log = audit or AuditLog()
    return (
        AgentRuntime(
            PolicyEngine(gateway.definitions),
            gateway,
            audit_log,
            trace_exporter=trace_exporter,
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
