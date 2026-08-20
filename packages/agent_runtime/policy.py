"""Fail-closed policy checks for Agent-to-tool execution."""

from __future__ import annotations

from collections.abc import Mapping

from .models import IdentityContext, PolicyDecision, ToolDefinition, ToolRisk


class PolicyEngine:
    """Authorize a tool using identity, workspace scope, role and tool risk."""

    def __init__(
        self,
        tools: Mapping[str, ToolDefinition],
        *,
        role_allowlist: Mapping[str, frozenset[str] | set[str]] | None = None,
    ) -> None:
        self._tools = dict(tools)
        configured_roles = role_allowlist or {
            "customer": frozenset({"erp.get_order_status"}),
            "support": frozenset({"erp.get_order_status"}),
            "sales": frozenset({"erp.get_order_status"}),
            "manager": frozenset({"erp.get_order_status"}),
            "admin": frozenset({"erp.get_order_status"}),
        }
        self._role_allowlist = {role: frozenset(tools) for role, tools in configured_roles.items()}

    def authorize(
        self,
        identity: IdentityContext,
        tool_name: str,
        *,
        approval_token: str | None = None,
    ) -> PolicyDecision:
        """Return an explicit decision; missing context always denies."""

        if not all(
            (
                identity.user_id.strip(),
                identity.workspace_id.strip(),
                identity.tenant_id.strip(),
                identity.role.strip(),
            )
        ):
            return PolicyDecision("deny", "identity and workspace scope are required", tool_name)

        definition = self._tools.get(tool_name)
        if definition is None:
            return PolicyDecision("deny", "tool is not registered", tool_name)

        allowed_tools = self._role_allowlist.get(identity.role)
        if allowed_tools is None or tool_name not in allowed_tools:
            return PolicyDecision(
                "deny", "role is not allowed to use this tool", tool_name, definition.risk
            )

        if definition.risk is ToolRisk.READ:
            return PolicyDecision("allow", "authorized read operation", tool_name, definition.risk)

        if approval_token:
            return PolicyDecision(
                "allow", "approved high-risk operation", tool_name, definition.risk
            )

        return PolicyDecision(
            "approval_required", "high-risk operation requires approval", tool_name, definition.risk
        )
