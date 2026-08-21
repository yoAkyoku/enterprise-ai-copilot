from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch

from packages.agent_runtime import (
    AgentRuntime,
    AuditLog,
    IdentityContext,
    InMemoryMcpGateway,
    ModelCompletion,
    ModelProviderError,
    OpenAICompatibleModelProvider,
    PolicyEngine,
    RunStatus,
)
from services.bootstrap import build_model_provider


class _Response:
    def __init__(self, body: dict[str, object]) -> None:
        self._body = json.dumps(body).encode("utf-8")

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, _limit: int) -> bytes:
        return self._body


class _Opener:
    def __init__(self, response: _Response) -> None:
        self.response = response
        self.request = None

    def open(self, request: object, timeout: float) -> _Response:
        self.request = request
        self.timeout = timeout
        return self.response


class _FakeModel:
    provider_id = "fake-model"
    model = "fake-1"
    requires_external_consent = True

    def __init__(self, error: bool = False) -> None:
        self.calls = 0
        self.error = error

    def health(self) -> dict[str, str]:
        return {"provider": self.provider_id, "model": self.model, "status": "configured"}

    def complete(
        self,
        query: str,
        evidence: dict[str, str],
        *,
        request_id: str,
        trace_id: str,
        run_id: str,
    ) -> ModelCompletion:
        del query, evidence, request_id, trace_id, run_id
        self.calls += 1
        if self.error:
            raise ModelProviderError("provider unavailable")
        return ModelCompletion(self.provider_id, self.model, "The shipment is moving.")


def _runtime(model: _FakeModel | None = None) -> tuple[AgentRuntime, AuditLog]:
    gateway = InMemoryMcpGateway(
        {
            ("demo-tenant", "SO-1001"): {
                "order_id": "SO-1001",
                "status": "in_transit",
            }
        }
    )
    audit = AuditLog()
    return AgentRuntime(
        PolicyEngine(gateway.definitions), gateway, audit, model_provider=model
    ), audit


class ModelProviderTests(unittest.TestCase):
    def test_production_model_configuration_fails_closed(self) -> None:
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(RuntimeError):
            build_model_provider("production")
        with (
            patch.dict(
                os.environ,
                {
                    "AGENT_MODEL_ENDPOINT": "https://model.example/v1",
                    "AGENT_MODEL_API_KEY": "secret",
                    "AGENT_MODEL_NAME": "model",
                },
                clear=True,
            ),
            self.assertRaises(ValueError),
        ):
            build_model_provider("production")

    def test_endpoint_requires_https_and_exact_allowlist(self) -> None:
        with self.assertRaises(ValueError):
            OpenAICompatibleModelProvider(
                "http://model.example/v1",
                "secret",
                "model",
                allowed_hosts=["model.example"],
            )
        provider = OpenAICompatibleModelProvider(
            "https://model.example/v1",
            "secret",
            "model",
            allowed_hosts=["model.example"],
        )
        self.assertEqual(provider.endpoint, "https://model.example/v1/chat/completions")

    def test_provider_sends_only_bounded_evidence_and_trace_headers(self) -> None:
        opener = _Opener(_Response({"choices": [{"message": {"content": "bounded"}}]}))
        provider = OpenAICompatibleModelProvider(
            "https://model.example/v1",
            "secret",
            "model",
            allowed_hosts=["model.example"],
        )
        with patch("packages.agent_runtime.model.urllib.request.build_opener", return_value=opener):
            completion = provider.complete(
                "Where is it?",
                {
                    "order_id": "SO-1001",
                    "status": "in_transit",
                    "source_id": "erp:SO-1001",
                    "observed_at": "2026-08-21T00:00:00Z",
                },
                request_id="request-1",
                trace_id="trace-1",
                run_id="run-1",
            )
        self.assertEqual(completion.text, "bounded")
        self.assertEqual(opener.timeout, 30.0)
        body = json.loads(opener.request.data)
        self.assertEqual(
            body["messages"][1]["content"],
            json.dumps(
                {
                    "query": "Where is it?",
                    "verified_evidence": {
                        "order_id": "SO-1001",
                        "status": "in_transit",
                        "source_id": "erp:SO-1001",
                        "observed_at": "2026-08-21T00:00:00Z",
                    },
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        )
        self.assertEqual(opener.request.headers["X-request-id"], "request-1")
        self.assertEqual(opener.request.headers["X-trace-id"], "trace-1")
        self.assertNotIn("query=", provider.endpoint)

    def test_runtime_requires_consent_before_model_call(self) -> None:
        model = _FakeModel()
        runtime, audit = _runtime(model)
        result = runtime.run(
            "Where is it?",
            IdentityContext("user", "workspace", "demo-tenant", "customer"),
            order_id="SO-1001",
        )
        self.assertEqual(result.status, RunStatus.SUCCEEDED)
        self.assertIn("Model explanation was not requested", result.message)
        self.assertEqual(model.calls, 0)
        self.assertEqual(audit.events[-1].event_type, "run.succeeded")

    def test_runtime_labels_model_text_and_preserves_verified_source(self) -> None:
        model = _FakeModel()
        runtime, _audit = _runtime(model)
        result = runtime.run(
            "Where is it?",
            IdentityContext("user", "workspace", "demo-tenant", "customer"),
            order_id="SO-1001",
            allow_external_model_processing=True,
        )
        self.assertEqual(result.status, RunStatus.SUCCEEDED)
        self.assertIn("Model explanation (unverified): The shipment is moving.", result.message)
        self.assertTrue(result.source_id)
        self.assertEqual(model.calls, 1)

    def test_model_failure_is_partial_success_not_false_confirmation(self) -> None:
        runtime, _audit = _runtime(_FakeModel(error=True))
        result = runtime.run(
            "Where is it?",
            IdentityContext("user", "workspace", "demo-tenant", "customer"),
            order_id="SO-1001",
            allow_external_model_processing=True,
        )
        self.assertEqual(result.status, RunStatus.PARTIAL_SUCCESS)
        self.assertIn("Model explanation is unavailable", result.message)
        self.assertIn("Order SO-1001 is in_transit", result.message)


if __name__ == "__main__":
    unittest.main()
