"""Shared outbound endpoint checks for provider and MCP adapters."""

from __future__ import annotations

import ipaddress
import urllib.parse
import urllib.request
from collections.abc import Sequence

_BLOCKED_HOSTNAMES = {
    "localhost",
    "localhost.localdomain",
    "metadata",
    "metadata.google.internal",
    "instance-data.ec2.internal",
}


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Prevent an approved HTTPS request from escaping to a new host."""

    def redirect_request(
        self,
        request: urllib.request.Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> None:
        del request, fp, code, msg, headers, newurl


def is_disallowed_host(host: str) -> bool:
    """Reject literal private targets and well-known cloud metadata names."""

    normalized = host.rstrip(".").lower()
    if normalized in _BLOCKED_HOSTNAMES or normalized.endswith(".localhost"):
        return True
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        return False
    return not address.is_global


def validated_https_endpoint(
    endpoint: str,
    allowed_hosts: Sequence[str],
    *,
    label: str,
    default_path: str = "/",
) -> str:
    """Validate an HTTPS endpoint and return a sanitized URL without secrets."""

    parsed = urllib.parse.urlparse(endpoint)
    host = (parsed.hostname or "").lower().rstrip(".")
    approved_hosts = {item.strip().lower().rstrip(".") for item in allowed_hosts if item.strip()}
    if (
        parsed.scheme != "https"
        or not host
        or host not in approved_hosts
        or is_disallowed_host(host)
    ):
        raise ValueError(f"{label} endpoint must be HTTPS, public and match an approved host")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError(f"{label} endpoint must not embed credentials or query values")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"{label} endpoint port is invalid") from exc
    if port is not None and not 1 <= port <= 65535:
        raise ValueError(f"{label} endpoint port is invalid")
    netloc = host if port is None else f"{host}:{port}"
    path = parsed.path or default_path
    return urllib.parse.urlunparse(("https", netloc, path.rstrip("/") or "/", "", "", ""))
