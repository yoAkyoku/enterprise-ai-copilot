"""Run explicit, synthetic-payload smoke checks against configured providers.

This command is intentionally opt-in. It can make billable model/Vision calls,
read an ERP order, upload/delete one namespaced object, scan bytes and export a
trace. It never runs in CI by default and refuses to run without
``--confirm-live``. Results contain no credentials or provider response text.
"""

from __future__ import annotations

import argparse
import os
from datetime import UTC, datetime
from uuid import uuid4

from packages.agent_runtime import IdentityContext
from packages.agent_runtime.models import ToolCallRequest
from packages.agent_runtime.network import validated_https_endpoint
from packages.attachments import ClamAvScanner, S3BlobStore
from packages.observability import OtlpHttpTraceExporter, TraceRecord
from packages.vision import OpenAICompatibleVisionProvider
from services.bootstrap import build_model_provider, build_erp_tool_definitions
from packages.agent_runtime import StreamableHttpMcpGateway


def _require(value: str, name: str) -> str:
    if not value.strip():
        raise RuntimeError(f"{name} is not configured")
    return value.strip()


def _smoke_order_id() -> str:
    order_id = os.getenv("AGENT_ACCEPTANCE_ORDER_ID", "SMOKE-0001").strip()
    if not order_id or len(order_id) > 128:
        raise RuntimeError("AGENT_ACCEPTANCE_ORDER_ID is invalid")
    return order_id


def _model() -> None:
    provider = build_model_provider(os.getenv("AGENT_PLATFORM_ENV", "development"))
    if provider is None:
        raise RuntimeError("model provider is not configured")
    result = provider.complete(
        "Return a short confirmation that the provider smoke test ran.",
        {
            "order_id": "SMOKE-0001",
            "status": "test_only",
            "source_id": "smoke-fixture",
            "observed_at": datetime.now(UTC).isoformat(),
        },
        request_id=f"smoke-{uuid4().hex}",
        trace_id=f"smoke-{uuid4().hex}",
        run_id=f"smoke-{uuid4().hex}",
    )
    if not result.text.strip():
        raise RuntimeError("model provider returned empty output")


def _mcp() -> None:
    endpoint = _require(os.getenv("AGENT_MCP_ENDPOINT", ""), "AGENT_MCP_ENDPOINT")
    gateway = StreamableHttpMcpGateway(
        endpoint,
        build_erp_tool_definitions(),
        allowed_hosts=os.getenv("AGENT_MCP_ALLOWED_HOSTS", "").split(","),
        bearer_token=os.getenv("AGENT_MCP_BEARER_TOKEN") or None,
        timeout_seconds=float(os.getenv("AGENT_MCP_TIMEOUT_SECONDS", "10")),
    )
    identity = IdentityContext(
        _require(os.getenv("AGENT_WORKER_USER_ID", ""), "AGENT_WORKER_USER_ID"),
        _require(os.getenv("AGENT_WORKER_WORKSPACE_ID", ""), "AGENT_WORKER_WORKSPACE_ID"),
        _require(os.getenv("AGENT_WORKER_TENANT_ID", ""), "AGENT_WORKER_TENANT_ID"),
        _require(os.getenv("AGENT_WORKER_ROLE", ""), "AGENT_WORKER_ROLE"),
    )
    order_id = _smoke_order_id()
    result = gateway.call(
        ToolCallRequest(
            request_id=f"smoke-{uuid4().hex}",
            trace_id=f"smoke-{uuid4().hex}",
            run_id=f"smoke-{uuid4().hex}",
            identity=identity,
            tool_name="erp.get_order_status",
            arguments={"order_id": order_id},
            idempotency_key=f"smoke-{uuid4().hex}",
        )
    )
    if not result.success or not all((result.source_id, result.observed_at, result.external_ref)):
        raise RuntimeError("MCP smoke response was not a successful provenance-bearing result")


def _vision() -> None:
    endpoint = _require(os.getenv("AGENT_VISION_ENDPOINT", ""), "AGENT_VISION_ENDPOINT")
    provider = OpenAICompatibleVisionProvider(
        endpoint,
        _require(os.getenv("AGENT_VISION_API_KEY", ""), "AGENT_VISION_API_KEY"),
        _require(os.getenv("AGENT_VISION_MODEL", ""), "AGENT_VISION_MODEL"),
        allowed_hosts=os.getenv("AGENT_VISION_ALLOWED_HOSTS", "").split(","),
        timeout_seconds=float(os.getenv("AGENT_VISION_TIMEOUT_SECONDS", "30")),
    )
    # A 1x1 transparent PNG is synthetic and contains no customer data.
    image = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d49444154789c6360000000020001e221bc33000000000049454e44ae426082"
    )
    text = provider.analyze(
        image, "image/png", task="describe", prompt="Return a short smoke-test confirmation."
    )
    if not text.strip():
        raise RuntimeError("Vision provider returned empty output")


def _clamav() -> None:
    scanner = ClamAvScanner(
        _require(os.getenv("AGENT_CLAMAV_HOST", ""), "AGENT_CLAMAV_HOST"),
        int(os.getenv("AGENT_CLAMAV_PORT", "3310")),
        timeout_seconds=float(os.getenv("AGENT_CLAMAV_TIMEOUT_SECONDS", "10")),
    )
    scanner.scan(b"enterprise-ai-copilot connector smoke; no customer data", "text/plain")


def _trace() -> None:
    exporter = OtlpHttpTraceExporter(
        _require(os.getenv("AGENT_TRACE_ENDPOINT", ""), "AGENT_TRACE_ENDPOINT"),
        allowed_hosts=os.getenv("AGENT_TRACE_ALLOWED_HOSTS", "").split(","),
        bearer_token=os.getenv("AGENT_TRACE_BEARER_TOKEN") or None,
        timeout_seconds=float(os.getenv("AGENT_TRACE_TIMEOUT_SECONDS", "5")),
    )
    now = datetime.now(UTC).timestamp()
    exporter.export(
        TraceRecord(
            trace_id=f"smoke-{uuid4().hex}",
            span_id=f"smoke-{uuid4().hex}",
            name="connector.smoke",
            start_time_unix_nano=int(now * 1_000_000_000),
            end_time_unix_nano=int(now * 1_000_000_000) + 1,
            attributes={"smoke": True, "environment": "operator"},
        )
    )


def _s3() -> None:
    import boto3

    bucket = _require(os.getenv("AGENT_S3_BUCKET", ""), "AGENT_S3_BUCKET")
    endpoint = os.getenv("AGENT_S3_ENDPOINT") or None
    if endpoint:
        endpoint = validated_https_endpoint(
            endpoint,
            os.getenv("AGENT_S3_ALLOWED_HOSTS", "").split(","),
            label="S3",
        )
    client = boto3.client(
        "s3", endpoint_url=endpoint, region_name=os.getenv("AGENT_S3_REGION") or None
    )
    store = S3BlobStore(
        client,
        bucket,
        prefix=os.getenv("AGENT_S3_PREFIX", "attachments"),
        kms_key_id=os.getenv("AGENT_S3_KMS_KEY_ID") or None,
    )
    key = f"connector-smoke/{uuid4().hex}.txt"
    store.put(key, b"enterprise-ai-copilot connector smoke", "text/plain")
    try:
        if store.read(key, 1024) != b"enterprise-ai-copilot connector smoke":
            raise RuntimeError("object storage returned unexpected smoke bytes")
    finally:
        store.delete(key)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run explicit live connector smoke checks")
    parser.add_argument(
        "--confirm-live",
        action="store_true",
        help="acknowledge external calls and one temporary object",
    )
    parser.add_argument(
        "--only", choices=("model", "mcp", "vision", "clamav", "trace", "s3", "all"), default="all"
    )
    args = parser.parse_args(argv)
    if not args.confirm_live:
        parser.error("refusing external calls without --confirm-live")
    checks = {
        "model": _model,
        "mcp": _mcp,
        "vision": _vision,
        "clamav": _clamav,
        "trace": _trace,
        "s3": _s3,
    }
    selected = checks.keys() if args.only == "all" else (args.only,)
    for name in selected:
        try:
            checks[name]()
        except Exception as exc:  # noqa: BLE001 - keep output safe and continue to summary
            print(f"{name}: FAIL ({type(exc).__name__})")
            return 1
        print(f"{name}: PASS")
    print("connector-smoke: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
