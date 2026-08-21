from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from scripts.production_preflight import _static_checks, main


class ProductionPreflightTests(unittest.TestCase):
    def _environment(self, **overrides: str) -> dict[str, str]:
        values = {
            "AGENT_PLATFORM_ENV": "production",
            "AGENT_PROVIDER_MODE": "remote",
            "AGENT_STORAGE_MODE": "postgres",
            "AGENT_DATABASE_URL": "postgresql://agent:secret-reference@postgres.example.com:5432/agent",
            "AGENT_REDIS_URL": "rediss://redis.example.com:6380/0",
            "AGENT_TRACE_ENDPOINT": "https://otel.example.com/v1/traces",
            "AGENT_TRACE_ALLOWED_HOSTS": "otel.example.com",
            "AGENT_MCP_ENDPOINT": "https://mcp.example.com/mcp",
            "AGENT_MCP_ALLOWED_HOSTS": "mcp.example.com",
            "AGENT_MCP_BEARER_TOKEN": "secret-reference",
            "AGENT_MODEL_ENDPOINT": "https://model.example.com/v1",
            "AGENT_MODEL_API_KEY": "secret-reference",
            "AGENT_MODEL_NAME": "reviewed-model",
            "AGENT_MODEL_ALLOWED_HOSTS": "model.example.com",
            "AGENT_AUTH_MODE": "oidc_jwks",
            "AGENT_OIDC_ISSUER": "https://identity.example.com",
            "AGENT_OIDC_AUDIENCE": "enterprise-agent-api",
            "AGENT_OIDC_JWKS_URI": "https://identity.example.com/.well-known/jwks.json",
            "AGENT_OIDC_ALLOWED_HOSTS": "identity.example.com",
            "AGENT_ATTACHMENT_STORAGE": "s3",
            "AGENT_MALWARE_SCANNER": "clamav",
            "AGENT_ATTACHMENT_RETENTION_DAYS": "30",
            "AGENT_S3_BUCKET": "copilot-prod",
            "AGENT_S3_REGION": "ap-northeast-1",
            "AGENT_S3_KMS_KEY_ID": "key-reference",
            "AGENT_S3_ENDPOINT": "https://s3.example.com",
            "AGENT_S3_ALLOWED_HOSTS": "s3.example.com",
            "AGENT_CLAMAV_HOST": "clamav.internal.example.com",
            "AGENT_WORKER_USER_ID": "worker-1",
            "AGENT_WORKER_WORKSPACE_ID": "workspace-1",
            "AGENT_WORKER_TENANT_ID": "tenant-1",
            "AGENT_WORKER_ROLE": "support",
        }
        values.update(overrides)
        return values

    def test_static_preflight_passes_without_opening_network(self) -> None:
        with patch.dict(os.environ, self._environment(), clear=True):
            checks = _static_checks()
        self.assertTrue(all(check.status in {"PASS", "NOT_APPLICABLE"} for check in checks), checks)
        self.assertIn("vision.optional", {check.name for check in checks})

    def test_placeholder_and_private_endpoint_fail_closed(self) -> None:
        environment = self._environment(
            AGENT_MCP_ENDPOINT="https://127.0.0.1/mcp",
            AGENT_MCP_ALLOWED_HOSTS="127.0.0.1",
            AGENT_MODEL_API_KEY="REPLACE_WITH_SECRET_REFERENCE",
        )
        with patch.dict(os.environ, environment, clear=True):
            checks = _static_checks()
        failures = {check.name for check in checks if check.status == "FAIL"}
        self.assertIn("AGENT_MODEL_API_KEY", failures)
        self.assertIn("network.MCP", failures)

    def test_cli_json_returns_failure_for_local_defaults(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(main(["--json"]), 1)


if __name__ == "__main__":
    unittest.main()
