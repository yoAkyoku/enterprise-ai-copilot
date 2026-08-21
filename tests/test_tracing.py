from __future__ import annotations

import json
import os
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from packages.agent_runtime import RunStatus
from packages.observability import (
    InMemoryTraceExporter,
    OtlpHttpTraceExporter,
    TraceRecord,
)
from services.api.app import create_app
from services.bootstrap import build_runtime, build_trace_exporter, demo_identity


class TracingTests(unittest.TestCase):
    def test_runtime_exports_tool_span_with_run_trace(self) -> None:
        exporter = InMemoryTraceExporter()
        runtime, _audit = build_runtime(trace_exporter=exporter)
        result = runtime.run(
            "show the order status",
            demo_identity(),
            order_id="SO-1001",
            trace_id="trace-test-1",
        )
        self.assertIs(result.status, RunStatus.SUCCEEDED)
        self.assertEqual(len(exporter.records), 1)
        self.assertEqual(exporter.records[0].trace_id, result.trace_id)
        self.assertEqual(exporter.records[0].name, "agent.tool")
        self.assertEqual(exporter.records[0].attributes["tool"], "erp.get_order_status")

    def test_http_and_tool_spans_share_trace_id(self) -> None:
        exporter = InMemoryTraceExporter()
        runtime, audit = build_runtime(trace_exporter=exporter)
        client = TestClient(
            create_app(runtime, audit, auth_mode="headers", trace_exporter=exporter)
        )
        response = client.post(
            "/api/v1/runs",
            headers={
                "X-User-Id": "user-a",
                "X-Workspace-Id": "demo-workspace",
                "X-Tenant-Id": "demo-tenant",
                "X-Role": "customer",
            },
            json={"query": "show order status", "order_id": "SO-1001"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertGreaterEqual(len(exporter.records), 2)
        self.assertEqual(
            {record.trace_id for record in exporter.records}, {response.json()["trace_id"]}
        )
        http_span = next(record for record in exporter.records if record.name == "http.request")
        self.assertEqual(http_span.attributes["http_route"], "/api/v1/runs")
        self.assertNotIn("SO-1001", str(http_span.attributes))

    def test_otlp_payload_is_bounded_and_sensitive_attributes_are_rejected(self) -> None:
        exporter = OtlpHttpTraceExporter(
            "https://otel.example.invalid/v1/traces",
            allowed_hosts=["otel.example.invalid"],
        )
        now = time.time_ns()
        record = TraceRecord(
            trace_id="trace-test-2",
            span_id="span-test-2",
            name="http.request",
            start_time_unix_nano=now,
            end_time_unix_nano=now + 1,
            attributes={"http_status": 200, "sampled": True},
        )
        payload = json.loads(exporter._payload(record))
        self.assertEqual(
            payload["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["name"], "http.request"
        )
        with self.assertRaises(ValueError):
            TraceRecord(
                trace_id="trace-test-3",
                span_id="span-test-3",
                name="unsafe",
                start_time_unix_nano=now,
                end_time_unix_nano=now + 1,
                attributes={"api_key": "must-not-be-exported"},
            )
        with self.assertRaises(TypeError):
            TraceRecord(
                trace_id="trace-test-4",
                span_id="span-test-4",
                name="unsafe",
                start_time_unix_nano=now,
                end_time_unix_nano=now + 1,
                attributes={1: "non-string-key"},  # type: ignore[dict-item]
            )

    def test_production_trace_configuration_fails_closed_and_accepts_allowlisted_endpoint(
        self,
    ) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(RuntimeError):
                build_trace_exporter("production")
        with patch.dict(
            os.environ,
            {
                "AGENT_TRACE_ENDPOINT": "https://otel.example.invalid/v1/traces",
                "AGENT_TRACE_ALLOWED_HOSTS": "otel.example.invalid",
            },
            clear=True,
        ):
            exporter = build_trace_exporter("production")
        self.assertIsInstance(exporter, OtlpHttpTraceExporter)


if __name__ == "__main__":
    unittest.main()
