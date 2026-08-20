"""In-memory MCP-compatible gateway used by the first vertical slice."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Protocol

from .models import ToolCallRequest, ToolDefinition, ToolResult, ToolRisk, utc_now


class McpGateway(Protocol):
    definitions: Mapping[str, ToolDefinition]

    def health(self) -> dict[str, str]:
        """Return an explicit transport health result."""

    def call(self, request: ToolCallRequest) -> ToolResult:
        """Execute a policy-approved, typed tool call."""


class InMemoryMcpGateway:
    """A deterministic fake ERP MCP server with tenant-scoped synthetic data."""

    def __init__(self, orders: Mapping[tuple[str, str], Mapping[str, str]]) -> None:
        self.server_id = "erp-demo"
        self.transport = "in_memory"
        self._orders = {key: dict(value) for key, value in orders.items()}
        self._definitions = {
            "erp.get_order_status": ToolDefinition(
                name="erp.get_order_status",
                risk=ToolRisk.READ,
                description="Return a tenant-scoped synthetic order status.",
            )
        }

    @property
    def definitions(self) -> Mapping[str, ToolDefinition]:
        return self._definitions

    def health(self) -> dict[str, str]:
        return {"server_id": self.server_id, "transport": self.transport, "status": "healthy"}

    def call(self, request: ToolCallRequest) -> ToolResult:
        """Execute only a registered, schema-checked, tenant-scoped tool."""

        definition = self._definitions.get(request.tool_name)
        if definition is None:
            return ToolResult(success=False, error="tool is not registered")

        if set(request.arguments) != {"order_id"}:
            return ToolResult(
                success=False, error="tool arguments do not match the registered schema"
            )

        order_id = request.arguments.get("order_id")
        if (
            not isinstance(order_id, str)
            or not order_id.strip()
            or not re.fullmatch(r"[A-Z0-9][A-Z0-9._-]{1,63}", order_id.strip())
        ):
            return ToolResult(success=False, error="order_id is required")

        order = self._orders.get((request.identity.tenant_id, order_id))
        if order is None:
            return ToolResult(
                success=False, error="order was not found in the authorized tenant scope"
            )

        observed_at = utc_now()
        return ToolResult(
            success=True,
            data=dict(order),
            source_id=f"erpnext:Sales Order:{order_id}",
            observed_at=observed_at,
            external_ref=order_id,
        )
