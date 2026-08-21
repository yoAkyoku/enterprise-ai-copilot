"""Run an opt-in, redacted acceptance probe against a deployed API.

The probe uses two real bearer tokens issued for different workspace/tenant
scopes. It verifies readiness, scoped dashboards, an end-to-end ERP run and
the attachment upload/read/delete boundary. It deliberately creates only two
synthetic 1x1 PNG attachments and removes them before returning.

This command is for an operator-controlled staging/production deployment. It
never accepts identity headers, follows redirects, prints tokens, or prints
provider response bodies. It refuses to make a network call without the
explicit ``--confirm-live`` acknowledgement.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from packages.agent_runtime.network import NoRedirectHandler, validated_https_endpoint

_MAX_RESPONSE_BYTES = 1024 * 1024
_MAX_TIMEOUT_SECONDS = 30.0
_SAFE_RESOURCE_ID = re.compile(r"^[A-Za-z0-9._:-]{1,256}$")
_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c6360000000020001e221bc33000000000049454e44ae426082"
)


class AcceptanceError(RuntimeError):
    """Raised when a deployed API fails an acceptance invariant."""


@dataclass(frozen=True)
class Principal:
    """A real bearer token and the scope it is expected to establish."""

    name: str
    token: str
    workspace_id: str
    tenant_id: str


@dataclass(frozen=True)
class Response:
    status: int
    body: bytes


class ApiClient:
    """Bounded, redirect-free HTTP client for the deployed API."""

    def __init__(self, base_url: str, *, allowed_hosts: list[str], timeout_seconds: float) -> None:
        if timeout_seconds <= 0 or timeout_seconds > _MAX_TIMEOUT_SECONDS:
            raise ValueError("acceptance timeout must be between 0 and 30 seconds")
        self.base_url = validated_https_endpoint(
            base_url, allowed_hosts, label="acceptance API", default_path="/"
        ).rstrip("/")
        self.timeout_seconds = timeout_seconds
        self._opener = urllib.request.build_opener(NoRedirectHandler)

    def request(
        self,
        method: str,
        path: str,
        *,
        token: str | None = None,
        body: bytes | None = None,
        content_type: str | None = None,
        idempotency_key: str | None = None,
    ) -> Response:
        if not path.startswith("/") or "?" in path or "#" in path:
            raise ValueError("acceptance paths must be fixed origin-relative paths")
        headers = {
            "Accept": "application/json",
            "X-Request-Id": f"acceptance-{secrets.token_hex(16)}",
        }
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        if content_type is not None:
            headers["Content-Type"] = content_type
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        request = urllib.request.Request(
            self.base_url + path,
            data=body,
            headers=headers,
            method=method,
        )
        try:
            with self._opener.open(request, timeout=self.timeout_seconds) as response:
                payload = response.read(_MAX_RESPONSE_BYTES + 1)
                if len(payload) > _MAX_RESPONSE_BYTES:
                    raise AcceptanceError("API response exceeded the acceptance size limit")
                return Response(response.status, payload)
        except urllib.error.HTTPError as exc:
            exc.close()
            return Response(exc.code, b"")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise AcceptanceError("API request failed") from exc

    @staticmethod
    def multipart_image() -> tuple[bytes, str]:
        boundary = f"----enterprise-agent-acceptance-{secrets.token_hex(12)}"
        body = (
            (
                f"--{boundary}\r\n"
                'Content-Disposition: form-data; name="image"; filename="acceptance.png"\r\n'
                "Content-Type: image/png\r\n\r\n"
            ).encode("ascii")
            + _PNG
            + f"\r\n--{boundary}--\r\n".encode("ascii")
        )
        return body, f"multipart/form-data; boundary={boundary}"


def _json(response: Response, label: str) -> dict[str, Any]:
    if not response.body:
        raise AcceptanceError(f"{label} returned an empty response")
    try:
        value = json.loads(response.body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AcceptanceError(f"{label} returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise AcceptanceError(f"{label} returned an invalid JSON object")
    return value


def _expect(response: Response, status: int, label: str) -> None:
    if response.status != status:
        raise AcceptanceError(f"{label} returned HTTP {response.status}, expected {status}")


def _required(name: str, *, maximum: int = 256) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"{name} is required")
    if len(value) > maximum:
        raise ValueError(f"{name} is too long")
    return value


def _principal(prefix: str, name: str) -> Principal:
    return Principal(
        name=name,
        token=_required(f"{prefix}_BEARER_TOKEN", maximum=8192),
        workspace_id=_required(f"{prefix}_WORKSPACE_ID"),
        tenant_id=_required(f"{prefix}_TENANT_ID"),
    )


def load_configuration() -> tuple[ApiClient, Principal, Principal, str]:
    """Load non-secret config and secret tokens without exposing their values."""

    base_url = _required("AGENT_ACCEPTANCE_BASE_URL", maximum=2048)
    allowed_hosts = [
        item.strip()
        for item in os.getenv("AGENT_ACCEPTANCE_ALLOWED_HOSTS", "").split(",")
        if item.strip()
    ]
    try:
        timeout_seconds = float(os.getenv("AGENT_ACCEPTANCE_TIMEOUT_SECONDS", "10"))
    except ValueError as exc:
        raise ValueError("AGENT_ACCEPTANCE_TIMEOUT_SECONDS must be numeric") from exc
    principal_a = _principal("AGENT_ACCEPTANCE_A", "principal-a")
    principal_b = _principal("AGENT_ACCEPTANCE_B", "principal-b")
    if principal_a.token == principal_b.token:
        raise ValueError("acceptance principals must use different bearer tokens")
    if (principal_a.workspace_id, principal_a.tenant_id) == (
        principal_b.workspace_id,
        principal_b.tenant_id,
    ):
        raise ValueError("acceptance principals must use different scopes")
    order_id = _required("AGENT_ACCEPTANCE_ORDER_ID", maximum=128)
    return (
        ApiClient(base_url, allowed_hosts=allowed_hosts, timeout_seconds=timeout_seconds),
        principal_a,
        principal_b,
        order_id,
    )


def _dashboard(client: ApiClient, principal: Principal) -> None:
    response = client.request("GET", "/api/v1/dashboard", token=principal.token)
    _expect(response, 200, f"dashboard.{principal.name}")
    payload = _json(response, f"dashboard.{principal.name}")
    if payload.get("workspace_id") != principal.workspace_id:
        raise AcceptanceError(f"dashboard.{principal.name} returned the wrong workspace")
    if payload.get("tenant_id") != principal.tenant_id:
        raise AcceptanceError(f"dashboard.{principal.name} returned the wrong tenant")


def _upload(client: ApiClient, principal: Principal) -> str:
    body, content_type = client.multipart_image()
    response = client.request(
        "POST",
        "/api/v1/attachments",
        token=principal.token,
        body=body,
        content_type=content_type,
    )
    _expect(response, 201, f"attachment.upload.{principal.name}")
    payload = _json(response, f"attachment.upload.{principal.name}")
    attachment_id = payload.get("id")
    if not isinstance(attachment_id, str) or not _SAFE_RESOURCE_ID.fullmatch(attachment_id):
        raise AcceptanceError(f"attachment.upload.{principal.name} returned an invalid id")
    return attachment_id


def _list_ids(client: ApiClient, principal: Principal) -> set[str]:
    response = client.request("GET", "/api/v1/attachments", token=principal.token)
    _expect(response, 200, f"attachment.list.{principal.name}")
    payload = _json(response, f"attachment.list.{principal.name}")
    items = payload.get("items")
    if not isinstance(items, list):
        raise AcceptanceError(f"attachment.list.{principal.name} returned invalid items")
    result: set[str] = set()
    for item in items:
        if isinstance(item, dict) and isinstance(item.get("id"), str):
            result.add(item["id"])
    return result


def _run_order(client: ApiClient, principal: Principal, order_id: str) -> str:
    body = json.dumps(
        {
            "query": "Where is my order?",
            "order_id": order_id,
            "allow_external_processing": False,
        },
        separators=(",", ":"),
    ).encode("utf-8")
    key = f"acceptance-{secrets.token_hex(16)}"
    response = client.request(
        "POST",
        "/api/v1/runs",
        token=principal.token,
        body=body,
        content_type="application/json",
        idempotency_key=key,
    )
    _expect(response, 200, "run.erp")
    payload = _json(response, "run.erp")
    if payload.get("status") != "succeeded":
        raise AcceptanceError("run.erp did not return succeeded")
    run_id = payload.get("run_id")
    if not isinstance(run_id, str) or not _SAFE_RESOURCE_ID.fullmatch(run_id):
        raise AcceptanceError("run.erp returned an invalid run id")
    return run_id


def run_acceptance(
    client: ApiClient,
    principal_a: Principal,
    principal_b: Principal,
    order_id: str,
) -> dict[str, object]:
    """Execute the deployed API invariants and return only safe evidence."""

    checks: list[dict[str, str]] = []
    for path, label in (("/health", "api.health"), ("/ready", "api.ready")):
        response = client.request("GET", path)
        _expect(response, 200, label)
        payload = _json(response, label)
        expected_status = "ok" if path == "/health" else "ready"
        if payload.get("status") != expected_status:
            raise AcceptanceError(f"{label} did not report {expected_status}")
        checks.append({"name": label, "status": "PASS"})

    _dashboard(client, principal_a)
    _dashboard(client, principal_b)
    checks.append({"name": "auth.scope", "status": "PASS"})

    run_id = _run_order(client, principal_a, order_id)
    response = client.request("GET", f"/api/v1/runs/{run_id}", token=principal_a.token)
    _expect(response, 200, "run.scope.owner")
    response = client.request("GET", f"/api/v1/runs/{run_id}", token=principal_b.token)
    _expect(response, 404, "run.scope.cross_tenant")
    checks.append({"name": "erp.end_to_end_and_run_isolation", "status": "PASS"})

    attachment_a: str | None = None
    attachment_b: str | None = None
    cleanup_failures = 0
    try:
        attachment_a = _upload(client, principal_a)
        attachment_b = _upload(client, principal_b)
        ids_a = _list_ids(client, principal_a)
        ids_b = _list_ids(client, principal_b)
        if attachment_a not in ids_a or attachment_b in ids_a:
            raise AcceptanceError("attachment list leaked across principal scopes")
        if attachment_b not in ids_b or attachment_a in ids_b:
            raise AcceptanceError("attachment list did not preserve principal scopes")
        for principal, own_id in ((principal_a, attachment_a), (principal_b, attachment_b)):
            response = client.request("GET", f"/api/v1/attachments/{own_id}", token=principal.token)
            _expect(response, 200, f"attachment.owner.{principal.name}")
            response = client.request(
                "GET", f"/api/v1/attachments/{own_id}/content", token=principal.token
            )
            _expect(response, 200, f"attachment.content.{principal.name}")
        for principal, foreign_id in ((principal_a, attachment_b), (principal_b, attachment_a)):
            response = client.request(
                "GET", f"/api/v1/attachments/{foreign_id}", token=principal.token
            )
            _expect(response, 404, f"attachment.cross_scope.{principal.name}")
            response = client.request(
                "GET", f"/api/v1/attachments/{foreign_id}/content", token=principal.token
            )
            _expect(response, 404, f"attachment.cross_content.{principal.name}")
        checks.append({"name": "attachment.tenant_isolation", "status": "PASS"})
    finally:
        for principal, attachment_id in (
            (principal_a, attachment_a),
            (principal_b, attachment_b),
        ):
            if attachment_id is None:
                continue
            try:
                response = client.request(
                    "DELETE", f"/api/v1/attachments/{attachment_id}", token=principal.token
                )
                if response.status not in {204, 404}:
                    cleanup_failures += 1
            except AcceptanceError:
                cleanup_failures += 1
    if cleanup_failures:
        raise AcceptanceError("attachment cleanup did not complete")
    checks.append({"name": "attachment.cleanup", "status": "PASS"})
    return {
        "schema": "enterprise-ai-copilot/production-api-acceptance/v1",
        "status": "PASS",
        "checks": checks,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run explicit live API acceptance checks")
    parser.add_argument(
        "--confirm-live",
        action="store_true",
        help="acknowledge requests, two temporary attachments and one ERP read",
    )
    parser.add_argument("--json-out", type=Path, help="write redacted JSON evidence to this path")
    args = parser.parse_args(argv)
    if not args.confirm_live:
        parser.error("refusing external calls without --confirm-live")
    try:
        client, principal_a, principal_b, order_id = load_configuration()
        result = run_acceptance(client, principal_a, principal_b, order_id)
    except (AcceptanceError, ValueError) as exc:
        result = {
            "schema": "enterprise-ai-copilot/production-api-acceptance/v1",
            "status": "FAIL",
            "error_type": type(exc).__name__,
        }
        if args.json_out is not None:
            args.json_out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(result, sort_keys=True))
        return 1
    if args.json_out is not None:
        args.json_out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
