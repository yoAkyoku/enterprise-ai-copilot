"""Fail-closed policy checks for Agent-to-tool execution."""

from __future__ import annotations

from collections.abc import Callable, Mapping

from .models import IdentityContext, PolicyDecision, ToolDefinition, ToolRisk

ApprovalVerifier = Callable[[IdentityContext, str, str, str, Mapping[str, object]], bool]


class PolicyEngine:
    """Authorize a tool using identity, workspace scope, role and tool risk."""

    def __init__(
        self,
        tools: Mapping[str, ToolDefinition],
        *,
        role_allowlist: Mapping[str, frozenset[str] | set[str]] | None = None,
        approval_verifier: ApprovalVerifier | None = None,
    ) -> None:
        self._tools = dict(tools)
        configured_roles = role_allowlist or self._role_allowlist_from_definitions(self._tools)
        self._role_allowlist = {role: frozenset(tools) for role, tools in configured_roles.items()}
        self._approval_verifier = approval_verifier

    @staticmethod
    def _role_allowlist_from_definitions(
        tools: Mapping[str, ToolDefinition],
    ) -> dict[str, frozenset[str]]:
        """Build a default role matrix from reviewed tool declarations.

        Read tools retain the first-slice convenience matrix. Higher-risk tools
        must explicitly declare their allowed roles in the typed definition;
        an omitted declaration therefore cannot accidentally grant write access.
        """

        roles = {role: set() for role in ("customer", "support", "sales", "manager", "admin")}
        for name, definition in tools.items():
            allowed_roles = definition.allowed_roles
            if not allowed_roles and definition.risk is ToolRisk.READ:
                allowed_roles = frozenset(roles)
            for role in allowed_roles:
                if role in roles:
                    roles[role].add(name)
        return {role: frozenset(names) for role, names in roles.items()}

    def authorize(
        self,
        identity: IdentityContext,
        tool_name: str,
        *,
        approval_id: str | None = None,
        approval_token: str | None = None,
        arguments: Mapping[str, object] | None = None,
    ) -> PolicyDecision:
        """Return an explicit decision; missing context always denies.

        A caller-supplied token is not proof of approval. Only a verifier bound
        to the durable ApprovalService may authorize a high-risk operation.
        """

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

        if (
            self._approval_verifier is not None
            and approval_id
            and approval_token
            and arguments is not None
        ):
            try:
                verified = self._approval_verifier(
                    identity, approval_id, approval_token, tool_name, arguments
                )
            except Exception:  # noqa: BLE001 - approval failures deny by default
                verified = False
            if verified:
                return PolicyDecision(
                    "allow", "approved high-risk operation", tool_name, definition.risk
                )

        return PolicyDecision(
            "approval_required", "high-risk operation requires approval", tool_name, definition.risk
        )
