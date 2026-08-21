"""MCP gateway contracts and a deterministic local ERP implementation."""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Mapping
from typing import Protocol
from uuid import uuid4

from .models import ToolCallRequest, ToolDefinition, ToolResult, ToolRisk, utc_now


class McpGateway(Protocol):
    definitions: Mapping[str, ToolDefinition]

    def health(self) -> dict[str, str]:
        """Return an explicit transport health result."""

    def call(self, request: ToolCallRequest) -> ToolResult:
        """Execute a policy-approved, typed tool call."""


def validate_tool_arguments(
    definition: ToolDefinition, arguments: Mapping[str, object]
) -> str | None:
    """Validate bounded JSON arguments against a reviewed tool definition."""

    if not isinstance(arguments, Mapping):
        return "tool arguments must be an object"
    try:
        encoded = json.dumps(
            dict(arguments), ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError):
        return "tool arguments must be valid JSON"
    if len(encoded) > 100_000:
        return "tool arguments exceed the size limit"
    schema = dict(definition.argument_schema)
    if not schema:
        return None
    if set(arguments) != set(schema):
        return "tool arguments do not match the registered schema"
    for name, expected in schema.items():
        value = arguments[name]
        valid = (
            (
                expected == "string"
                and isinstance(value, str)
                and bool(value.strip())
                and len(value) <= 4000
            )
            or (
                expected == "number"
                and isinstance(value, (int, float))
                and not isinstance(value, bool)
            )
            or (expected == "boolean" and isinstance(value, bool))
            or (expected == "object" and isinstance(value, Mapping))
            or (expected == "array" and isinstance(value, list))
        )
        if not valid:
            return f"tool argument {name!r} has the wrong type or value"
    return None


class InMemoryMcpGateway:
    """A deterministic fake ERP MCP server with tenant-scoped synthetic data."""

    def __init__(self, orders: Mapping[tuple[str, str], Mapping[str, str]]) -> None:
        self.server_id = "erp-demo"
        self.transport = "in_memory"
        self._orders = {key: dict(value) for key, value in orders.items()}
        self._returns: dict[tuple[str, str, str], ToolResult] = {}
        self._lock = threading.RLock()
        self._definitions = {
            "erp.get_order_status": ToolDefinition(
                name="erp.get_order_status",
                risk=ToolRisk.READ,
                description="Return a tenant-scoped synthetic order status.",
                argument_schema=(("order_id", "string"),),
            ),
            "erp.create_return": ToolDefinition(
                name="erp.create_return",
                risk=ToolRisk.WRITE,
                description="Create a reviewed return request for a tenant-scoped order.",
                allowed_roles=frozenset({"manager", "admin"}),
                argument_schema=(("order_id", "string"), ("reason", "string")),
            ),
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

        argument_error = validate_tool_arguments(definition, request.arguments)
        if argument_error:
            return ToolResult(success=False, error=argument_error)

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

        if request.tool_name == "erp.create_return":
            reason = request.arguments.get("reason")
            if not isinstance(reason, str) or not reason.strip() or len(reason.strip()) > 500:
                return ToolResult(success=False, error="return reason is required")
            key = (request.identity.tenant_id, request.tool_name, request.idempotency_key)
            with self._lock:
                existing = self._returns.get(key)
                if existing is not None:
                    return existing
                return_id = f"RET-{uuid4().hex[:16].upper()}"
                result = ToolResult(
                    success=True,
                    data={
                        "return_id": return_id,
                        "order_id": order_id,
                        "status": "requested",
                        "reason": reason.strip(),
                    },
                    source_id=f"erpnext:Return:{return_id}",
                    observed_at=utc_now(),
                    external_ref=return_id,
                    workspace_id=request.identity.workspace_id,
                    tenant_id=request.identity.tenant_id,
                )
                self._returns[key] = result
                return result

        observed_at = utc_now()
        return ToolResult(
            success=True,
            data=dict(order),
            source_id=f"erpnext:Sales Order:{order_id}",
            observed_at=observed_at,
            external_ref=order_id,
            workspace_id=request.identity.workspace_id,
            tenant_id=request.identity.tenant_id,
        )
