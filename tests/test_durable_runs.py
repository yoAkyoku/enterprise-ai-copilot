from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from packages.agent_runtime import IdentityContext, RunResult, RunStatus, SQLiteRunStore, StoredRun
from services.api.app import create_app
from services.bootstrap import build_runtime


class DurableRunStoreTests(unittest.TestCase):
    def test_run_survives_reopen_and_scope_is_enforced(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "platform.sqlite3"
            identity = IdentityContext("user-a", "workspace-a", "tenant-a", "customer")
            stored = StoredRun(
                result=RunResult(
                    status=RunStatus.SUCCEEDED,
                    run_id="run-1",
                    trace_id="trace-1",
                    agent_id="customer-service-agent",
                    message="verified",
                    source_id="erp-demo:SO-1001",
                ),
                identity=identity,
                idempotency_key="idem-1",
            )
            first = SQLiteRunStore(path)
            first.save(stored)
            first.close()

            reopened = SQLiteRunStore(path)
            try:
                self.assertEqual(reopened.get("run-1", identity), stored)
                self.assertEqual(reopened.find_idempotent(identity, "idem-1"), stored)
                other = IdentityContext("user-a", "workspace-a", "tenant-b", "customer")
                self.assertIsNone(reopened.get("run-1", other))
            finally:
                reopened.close()

    def test_api_reads_run_and_idempotency_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "platform.sqlite3"
            runtime, audit = build_runtime()
            identity_headers = {
                "X-User-Id": "demo-user",
                "X-Workspace-Id": "demo-workspace",
                "X-Tenant-Id": "demo-tenant",
                "X-Role": "customer",
                "Idempotency-Key": "restart-safe-1",
            }
            first_store = SQLiteRunStore(path)
            first_client = TestClient(
                create_app(runtime, audit, auth_mode="headers", run_store=first_store)
            )
            created = first_client.post(
                "/api/v1/runs",
                headers=identity_headers,
                json={"query": "Where is my order?", "order_id": "SO-1001"},
            )
            self.assertEqual(created.status_code, 200)
            run_id = created.json()["run_id"]
            first_store.close()

            second_store = SQLiteRunStore(path)
            try:
                second_client = TestClient(
                    create_app(runtime, audit, auth_mode="headers", run_store=second_store)
                )
                read_back = second_client.get(
                    f"/api/v1/runs/{run_id}",
                    headers={
                        key: value
                        for key, value in identity_headers.items()
                        if key != "Idempotency-Key"
                    },
                )
                self.assertEqual(read_back.status_code, 200)
                repeated = second_client.post(
                    "/api/v1/runs",
                    headers=identity_headers,
                    json={"query": "Where is my order?", "order_id": "SO-1001"},
                )
                self.assertEqual(repeated.status_code, 200)
                self.assertEqual(repeated.json()["run_id"], run_id)
            finally:
                second_store.close()


if __name__ == "__main__":
    unittest.main()
