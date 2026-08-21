"""Validate a production environment before starting the platform.

The default mode is static and never opens a socket. ``--live`` performs only
bounded, read-oriented dependency checks (except an explicitly requested trace
probe) and is intended for an operator-controlled target environment. The
script prints configuration-safe results; it never prints secret values.
"""

from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from packages.agent_runtime.network import is_disallowed_host, validated_https_endpoint


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: str


def _value(name: str) -> str:
    return os.getenv(name, "").strip()


def _placeholder(value: str) -> bool:
    lowered = value.lower()
    return not value or "example.invalid" in lowered or "replace_" in lowered


def _hosts(name: str) -> list[str]:
    return [item.strip() for item in _value(name).split(",") if item.strip()]


def _endpoint(name: str, allowlist_name: str, label: str, default_path: str = "/") -> str:
    endpoint = _value(name)
    if _placeholder(endpoint):
        raise ValueError(f"{name} is missing or still contains an example value")
    return validated_https_endpoint(
        endpoint,
        _hosts(allowlist_name),
        label=label,
        default_path=default_path,
    )


def _static_checks() -> list[Check]:
    checks: list[Check] = []

    def require(name: str, detail: str | None = None) -> None:
        value = _value(name)
        checks.append(
            Check(
                name,
                "PASS" if value and not _placeholder(value) else "FAIL",
                detail
                or (
                    "configured" if value and not _placeholder(value) else "missing or placeholder"
                ),
            )
        )

    env = _value("AGENT_PLATFORM_ENV").lower()
    checks.append(
        Check(
            "platform.environment",
            "PASS" if env in {"staging", "production"} else "FAIL",
            env or "AGENT_PLATFORM_ENV is missing",
        )
    )
    checks.append(
        Check(
            "provider.remote",
            "PASS" if _value("AGENT_PROVIDER_MODE").lower() == "remote" else "FAIL",
            _value("AGENT_PROVIDER_MODE") or "AGENT_PROVIDER_MODE is missing",
        )
    )
    storage_mode = _value("AGENT_STORAGE_MODE").lower()
    if storage_mode == "postgres":
        require("AGENT_DATABASE_URL")
        database_url = _value("AGENT_DATABASE_URL")
        parsed_database = urllib.parse.urlparse(database_url)
        checks.append(
            Check(
                "storage.postgres_url",
                "PASS"
                if parsed_database.scheme in {"postgres", "postgresql"}
                and bool(parsed_database.hostname)
                and not _placeholder(database_url)
                else "FAIL",
                "PostgreSQL DSN is configured"
                if parsed_database.scheme in {"postgres", "postgresql"}
                and bool(parsed_database.hostname)
                and not _placeholder(database_url)
                else "AGENT_DATABASE_URL must be a non-placeholder PostgreSQL DSN",
            )
        )
    elif storage_mode == "sqlite":
        checks.append(
            Check(
                "storage.sqlite_single_node",
                "PASS",
                "SQLite is explicitly selected; deployment is single-node only",
            )
        )
    else:
        checks.append(
            Check(
                "storage.mode",
                "FAIL",
                "production requires AGENT_STORAGE_MODE=postgres or sqlite",
            )
        )
    require("AGENT_REDIS_URL")
    require("AGENT_TRACE_ENDPOINT")
    require("AGENT_TRACE_ALLOWED_HOSTS")
    require("AGENT_MCP_ENDPOINT")
    require("AGENT_MCP_ALLOWED_HOSTS")
    require("AGENT_MCP_BEARER_TOKEN")
    require("AGENT_MODEL_ENDPOINT")
    require("AGENT_MODEL_API_KEY")
    require("AGENT_MODEL_NAME")
    require("AGENT_MODEL_ALLOWED_HOSTS")

    vision_values = tuple(
        _value(name)
        for name in ("AGENT_VISION_ENDPOINT", "AGENT_VISION_API_KEY", "AGENT_VISION_MODEL")
    )
    if any(vision_values):
        for name in (
            "AGENT_VISION_ENDPOINT",
            "AGENT_VISION_API_KEY",
            "AGENT_VISION_MODEL",
            "AGENT_VISION_ALLOWED_HOSTS",
        ):
            require(name)
        try:
            _endpoint(
                "AGENT_VISION_ENDPOINT",
                "AGENT_VISION_ALLOWED_HOSTS",
                "Vision",
                "/chat/completions",
            )
            checks.append(
                Check("network.vision", "PASS", "HTTPS endpoint is public and allowlisted")
            )
        except ValueError as exc:
            checks.append(Check("network.vision", "FAIL", str(exc)))
    else:
        checks.append(Check("vision.optional", "NOT_APPLICABLE", "Vision/OCR egress is disabled"))

    auth_mode = _value("AGENT_AUTH_MODE").lower()
    checks.append(
        Check(
            "identity.mode",
            "PASS" if auth_mode in {"oidc_jwks", "jwt_hs256"} else "FAIL",
            auth_mode or "production requires oidc_jwks or jwt_hs256",
        )
    )
    if auth_mode == "oidc_jwks":
        for name in (
            "AGENT_OIDC_ISSUER",
            "AGENT_OIDC_AUDIENCE",
            "AGENT_OIDC_JWKS_URI",
            "AGENT_OIDC_ALLOWED_HOSTS",
        ):
            require(name)
        try:
            _endpoint("AGENT_OIDC_ISSUER", "AGENT_OIDC_ALLOWED_HOSTS", "OIDC issuer")
            _endpoint("AGENT_OIDC_JWKS_URI", "AGENT_OIDC_ALLOWED_HOSTS", "OIDC JWKS")
            checks.append(
                Check(
                    "identity.endpoint_policy", "PASS", "OIDC endpoints are HTTPS and allowlisted"
                )
            )
        except ValueError as exc:
            checks.append(Check("identity.endpoint_policy", "FAIL", str(exc)))
    elif auth_mode == "jwt_hs256":
        secret = _value("AGENT_JWT_SECRET")
        checks.append(
            Check(
                "identity.secret",
                "PASS"
                if len(secret.encode("utf-8")) >= 32 and not _placeholder(secret)
                else "FAIL",
                "HS256 secret length is acceptable"
                if len(secret.encode("utf-8")) >= 32
                else "HS256 secret must be at least 32 bytes",
            )
        )
        require("AGENT_JWT_ISSUER")
        require("AGENT_JWT_AUDIENCE")

    storage = _value("AGENT_ATTACHMENT_STORAGE").lower()
    scanner = _value("AGENT_MALWARE_SCANNER").lower()
    retention = _value("AGENT_ATTACHMENT_RETENTION_DAYS")
    try:
        retention_ok = int(retention) > 0
    except ValueError:
        retention_ok = False
    checks.append(
        Check(
            "attachments.object_storage",
            "PASS" if storage == "s3" else "FAIL",
            storage or "S3 storage is required",
        )
    )
    checks.append(
        Check(
            "attachments.scanner",
            "PASS" if scanner == "clamav" else "FAIL",
            scanner or "ClamAV is required",
        )
    )
    checks.append(
        Check(
            "attachments.retention",
            "PASS" if retention_ok else "FAIL",
            retention or "positive retention is required",
        )
    )
    require("AGENT_S3_BUCKET")
    require("AGENT_S3_REGION")
    require("AGENT_S3_KMS_KEY_ID")
    require("AGENT_S3_ENDPOINT")
    require("AGENT_S3_ALLOWED_HOSTS")
    require("AGENT_CLAMAV_HOST")
    require("AGENT_WORKER_USER_ID")
    require("AGENT_WORKER_WORKSPACE_ID")
    require("AGENT_WORKER_TENANT_ID")
    require("AGENT_WORKER_ROLE")

    endpoints = (
        ("AGENT_MCP_ENDPOINT", "AGENT_MCP_ALLOWED_HOSTS", "MCP"),
        ("AGENT_MODEL_ENDPOINT", "AGENT_MODEL_ALLOWED_HOSTS", "model", "/v1/chat/completions"),
        ("AGENT_TRACE_ENDPOINT", "AGENT_TRACE_ALLOWED_HOSTS", "trace exporter", "/v1/traces"),
    )
    for item in endpoints:
        name, allowlist, label, *default = item
        try:
            _endpoint(name, allowlist, label, default[0] if default else "/")
            checks.append(
                Check(f"network.{label}", "PASS", "HTTPS endpoint is public and allowlisted")
            )
        except ValueError as exc:
            checks.append(Check(f"network.{label}", "FAIL", str(exc)))
    try:
        _endpoint("AGENT_S3_ENDPOINT", "AGENT_S3_ALLOWED_HOSTS", "S3")
        checks.append(Check("network.S3", "PASS", "HTTPS endpoint is public and allowlisted"))
    except ValueError as exc:
        checks.append(Check("network.S3", "FAIL", str(exc)))
    return checks


def _live_http(
    name: str,
    endpoint: str,
    *,
    method: str = "GET",
    bearer_token: str | None = None,
) -> Check:
    headers = {"Accept": "application/json"}
    if bearer_token:
        headers["Authorization"] = f"Bearer {bearer_token}"
    request = urllib.request.Request(endpoint, headers=headers, method=method)
    try:
        with urllib.request.build_opener(_NoRedirectHandler).open(request, timeout=5) as response:
            status = int(response.status)
            response.read(8192)
    except (OSError, urllib.error.URLError, ValueError) as exc:
        return Check(name, "FAIL", type(exc).__name__)
    # Streamable MCP endpoints commonly reject GET with 405 while still being
    # reachable. The protocol/provenance contract is exercised by
    # connector_smoke.py, not by this cheap dependency probe.
    return Check(name, "PASS" if 200 <= status < 400 or status == 405 else "FAIL", f"HTTP {status}")


def _live_s3() -> Check:
    """Probe the configured bucket without reading or writing an object."""

    try:
        import boto3
        from botocore.config import Config

        from packages.attachments import S3BlobStore

        endpoint = _endpoint("AGENT_S3_ENDPOINT", "AGENT_S3_ALLOWED_HOSTS", "S3")
        store = S3BlobStore(
            boto3.client(
                "s3",
                endpoint_url=endpoint,
                region_name=_value("AGENT_S3_REGION") or None,
                config=Config(
                    connect_timeout=5,
                    read_timeout=5,
                    retries={"max_attempts": 2, "mode": "standard"},
                ),
            ),
            _value("AGENT_S3_BUCKET"),
            prefix=_value("AGENT_S3_PREFIX") or "attachments",
            kms_key_id=_value("AGENT_S3_KMS_KEY_ID") or None,
        )
        healthy = store.healthcheck()
        return Check(
            "live.s3",
            "PASS" if healthy else "FAIL",
            "HeadBucket response received" if healthy else "bucket is unavailable",
        )
    except Exception as exc:  # noqa: BLE001 - report only a safe exception type
        return Check("live.s3", "FAIL", type(exc).__name__)


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        del request, fp, code, msg, headers, newurl
        return None


def _live_checks() -> list[Check]:
    checks: list[Check] = []
    try:
        from redis import Redis

        redis_url = _value("AGENT_REDIS_URL")
        parsed = urllib.parse.urlparse(redis_url)
        if (
            parsed.scheme not in {"redis", "rediss"}
            or not parsed.hostname
            or is_disallowed_host(parsed.hostname)
        ):
            raise ValueError("Redis URL must use redis/rediss and a non-private host")
        client = Redis.from_url(redis_url, socket_timeout=5, socket_connect_timeout=5)
        healthy = bool(client.ping())
        client.close()
        checks.append(Check("live.redis", "PASS" if healthy else "FAIL", "PING response received"))
    except (ImportError, OSError, ValueError) as exc:
        checks.append(Check("live.redis", "FAIL", type(exc).__name__))

    try:
        checks.append(
            _live_http(
                "live.mcp",
                _endpoint("AGENT_MCP_ENDPOINT", "AGENT_MCP_ALLOWED_HOSTS", "MCP"),
                bearer_token=_value("AGENT_MCP_BEARER_TOKEN"),
            )
        )
    except ValueError as exc:
        checks.append(Check("live.mcp", "FAIL", str(exc)))
    try:
        checks.append(
            _live_http(
                "live.oidc_jwks",
                _endpoint("AGENT_OIDC_JWKS_URI", "AGENT_OIDC_ALLOWED_HOSTS", "OIDC JWKS"),
            )
        )
    except ValueError:
        pass
    for name, endpoint_name, allowlist_name, label, bearer_name, default_path in (
        (
            "live.model",
            "AGENT_MODEL_ENDPOINT",
            "AGENT_MODEL_ALLOWED_HOSTS",
            "model",
            "AGENT_MODEL_API_KEY",
            "/v1/chat/completions",
        ),
        (
            "live.trace",
            "AGENT_TRACE_ENDPOINT",
            "AGENT_TRACE_ALLOWED_HOSTS",
            "trace exporter",
            "AGENT_TRACE_BEARER_TOKEN",
            "/v1/traces",
        ),
    ):
        try:
            checks.append(
                _live_http(
                    name,
                    _endpoint(endpoint_name, allowlist_name, label, default_path),
                    bearer_token=_value(bearer_name),
                )
            )
        except ValueError as exc:
            checks.append(Check(name, "FAIL", str(exc)))
    checks.append(_live_s3())
    try:
        from packages.attachments import ClamAvScanner

        scanner = ClamAvScanner(
            _value("AGENT_CLAMAV_HOST"),
            int(_value("AGENT_CLAMAV_PORT") or "3310"),
            timeout_seconds=5,
        )
        # A clamd PING is not exposed by the scanner contract; this harmless
        # payload validates the INSTREAM protocol and is not customer data.
        scanner.scan(b"enterprise-ai-copilot preflight", "text/plain")
        checks.append(Check("live.clamav", "PASS", "clamd accepted a bounded smoke payload"))
    except (ImportError, OSError, ValueError, RuntimeError) as exc:
        checks.append(Check("live.clamav", "FAIL", type(exc).__name__))
    return checks


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate Enterprise Agent production configuration"
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="also probe configured Redis, MCP, OIDC, model, trace, S3 and ClamAV endpoints",
    )
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = parser.parse_args(argv)
    checks = _static_checks()
    if args.live:
        checks.extend(_live_checks())
    result = {
        "status": "PASS"
        if all(check.status in {"PASS", "NOT_APPLICABLE"} for check in checks)
        else "FAIL",
        "checks": [check.__dict__ for check in checks],
    }
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        for check in checks:
            print(f"{check.status:6} {check.name}: {check.detail}")
        print(f"production-preflight: {result['status']}")
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
