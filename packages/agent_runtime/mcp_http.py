"""Safe Streamable HTTP JSON-RPC MCP gateway adapter."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping, Sequence

from .models import ToolCallRequest, ToolDefinition, ToolResult
from .network import NoRedirectHandler, validated_https_endpoint


class StreamableHttpMcpGateway:
    """Call an explicitly configured MCP endpoint without exposing credentials.

    The adapter sends authenticated identity context as trusted headers. The
    caller's arguments remain a separately validated tool payload; a model or
    user cannot choose the tenant header.
    """

    transport = "streamable_http"

    def __init__(
        self,
        endpoint: str,
        definitions: Mapping[str, ToolDefinition],
        *,
        allowed_hosts: Sequence[str],
        bearer_token: str | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        if timeout_seconds <= 0 or timeout_seconds > 120:
            raise ValueError("MCP timeout must be between 0 and 120 seconds")
        self.endpoint = validated_https_endpoint(
            endpoint, allowed_hosts, label="MCP", default_path="/"
        )
        self.server_id = "remote-mcp"
        self.definitions = dict(definitions)
        self._bearer_token = bearer_token
        self._timeout_seconds = timeout_seconds

    def health(self) -> dict[str, str]:
        request = urllib.request.Request(self.endpoint, headers=self._headers(), method="GET")
        try:
            opener = urllib.request.build_opener(NoRedirectHandler)
            with opener.open(request, timeout=self._timeout_seconds) as response:
                healthy = 200 <= response.status < 300
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError):
            healthy = False
        return {
            "server_id": self.server_id,
            "transport": self.transport,
            "status": "healthy" if healthy else "unhealthy",
        }

    def call(self, request: ToolCallRequest) -> ToolResult:
        definition = self.definitions.get(request.tool_name)
        if definition is None:
            return ToolResult(success=False, error="tool is not registered")
        payload = {
            "jsonrpc": "2.0",
            "id": request.request_id,
            "method": "tools/call",
            "params": {"name": request.tool_name, "arguments": request.arguments},
        }
        headers = self._headers()
        headers["Content-Type"] = "application/json"
        headers["Accept"] = "application/json, text/event-stream"
        headers["X-Request-Id"] = request.request_id
        headers["X-Trace-Id"] = request.trace_id
        headers["X-Run-Id"] = request.run_id
        headers["X-Workspace-Id"] = request.identity.workspace_id
        headers["X-Tenant-Id"] = request.identity.tenant_id
        headers["X-User-Id"] = request.identity.user_id
        outgoing = urllib.request.Request(
            self.endpoint,
            data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            opener = urllib.request.build_opener(NoRedirectHandler)
            with opener.open(outgoing, timeout=self._timeout_seconds) as response:
                raw = response.read(2 * 1024 * 1024 + 1)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError):
            return ToolResult(success=False, error="remote MCP request failed")
        if len(raw) > 2 * 1024 * 1024:
            return ToolResult(
                success=False, error="remote MCP response exceeded the response limit"
            )
        try:
            body = json.loads(raw)
            if not isinstance(body, dict) or body.get("error") is not None:
                return ToolResult(success=False, error="remote MCP returned an error")
            result = body.get("result")
            if not isinstance(result, dict):
                return ToolResult(success=False, error="remote MCP returned an invalid result")
            structured = result.get("structuredContent") or result.get("data")
            if not isinstance(structured, dict):
                return ToolResult(success=False, error="remote MCP result has no structured data")
            source_id = structured.get("source_id")
            observed_at = structured.get("observed_at")
            external_ref = structured.get("external_ref")
            data = structured.get("data", structured)
            if not isinstance(source_id, str) or not source_id.strip():
                return ToolResult(success=False, error="remote MCP result is missing provenance")
            if not isinstance(observed_at, str) or not observed_at.strip():
                return ToolResult(success=False, error="remote MCP result is missing observed time")
            if not isinstance(external_ref, str) or not external_ref.strip():
                return ToolResult(
                    success=False, error="remote MCP result is missing external reference"
                )
            if not isinstance(data, dict):
                return ToolResult(success=False, error="remote MCP result data is invalid")
            return ToolResult(
                success=True,
                data=data,
                source_id=source_id,
                observed_at=observed_at,
                external_ref=external_ref,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
            return ToolResult(success=False, error="remote MCP returned invalid JSON")

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self._bearer_token:
            headers["Authorization"] = f"Bearer {self._bearer_token}"
        return headers
