"""Opt-in PostgreSQL integration checks for a disposable database."""

from __future__ import annotations

import os
import unittest
import uuid
from datetime import UTC, datetime

from packages.agent_runtime.approvals import ApprovalRecord, ApprovalStatus
from packages.agent_runtime.models import (
    AuditEvent,
    IdentityContext,
    RunResult,
    RunStatus,
    ToolRisk,
)
from packages.agent_runtime.runs import StoredRun
from packages.attachments.service import AttachmentRecord
from packages.persistence import (
    PostgresApprovalStore,
    PostgresAttachmentStore,
    PostgresAuditStore,
    PostgresRunStore,
)
from scripts.migrate_postgres import main as migrate_postgres


@unittest.skipUnless(
    os.getenv("AGENT_TEST_POSTGRES_URL"),
    "set AGENT_TEST_POSTGRES_URL to a disposable PostgreSQL database",
)
class PostgresIntegrationTests(unittest.TestCase):
    """Exercise the shared-store contracts without requiring CI credentials."""

    def test_migrations_and_scoped_store_contracts(self) -> None:
        database_url = os.environ["AGENT_TEST_POSTGRES_URL"]
        self.assertEqual(migrate_postgres([database_url]), 0)
        suffix = uuid.uuid4().hex
        identity = IdentityContext(
            user_id=f"user-{suffix}",
            workspace_id=f"workspace-{suffix}",
            tenant_id=f"tenant-{suffix}",
            role="manager",
        )
        now = datetime.now(UTC).isoformat()
        audit = PostgresAuditStore(database_url)
        runs = PostgresRunStore(database_url)
        approvals = PostgresApprovalStore(database_url)
        attachments = PostgresAttachmentStore(database_url)
        try:
            self.assertTrue(audit.healthcheck())
            self.assertTrue(runs.healthcheck())
            self.assertTrue(approvals.healthcheck())
            self.assertTrue(attachments.healthcheck())
            audit.append(
                AuditEvent(
                    event_type="run.completed",
                    request_id=f"request-{suffix}",
                    trace_id=f"trace-{suffix}",
                    run_id=f"run-{suffix}",
                    workspace_id=identity.workspace_id,
                    agent_id="customer-service",
                    payload={"tenant_id": identity.tenant_id, "verified": True},
                )
            )
            self.assertEqual(
                len(
                    audit.list_events(
                        workspace_id=identity.workspace_id,
                        tenant_id=identity.tenant_id,
                    )
                ),
                1,
            )

            stored = StoredRun(
                result=RunResult(
                    status=RunStatus.SUCCEEDED,
                    run_id=f"run-{suffix}",
                    trace_id=f"trace-{suffix}",
                    agent_id="customer-service",
                    message="verified",
                    source_id="erp",
                    observed_at=now,
                ),
                identity=identity,
                idempotency_key=f"run-key-{suffix}",
            )
            self.assertEqual(runs.save(stored), stored)
            self.assertIsNotNone(runs.get(stored.result.run_id, identity))
            self.assertIsNone(
                runs.get(
                    stored.result.run_id,
                    IdentityContext(
                        "other-user", identity.workspace_id, identity.tenant_id, identity.role
                    ),
                )
            )
            self.assertIsNotNone(runs.find_idempotent(identity, stored.idempotency_key or ""))
            duplicate_store = PostgresRunStore(database_url)
            try:
                duplicate = duplicate_store.save(
                    StoredRun(
                        result=RunResult(
                            status=RunStatus.FAILED,
                            run_id=f"duplicate-{suffix}",
                            trace_id=f"duplicate-trace-{suffix}",
                            agent_id="customer-service",
                            message="must not replace existing result",
                        ),
                        identity=identity,
                        idempotency_key=stored.idempotency_key,
                    )
                )
                self.assertEqual(duplicate, stored)
            finally:
                duplicate_store.close()

            approval = ApprovalRecord(
                id=f"approval-{suffix}",
                requester=identity,
                tool_name="erp.create_return",
                arguments={"order_id": f"SO-{suffix}"},
                arguments_hash="synthetic-hash",
                risk=ToolRisk.WRITE,
                idempotency_key=f"approval-key-{suffix}",
                requested_at=now,
                expires_at=now,
                status=ApprovalStatus.PENDING,
            )
            approvals.create(approval)
            self.assertIsNotNone(
                approvals.find_idempotent(
                    workspace_id=identity.workspace_id,
                    tenant_id=identity.tenant_id,
                    user_id=identity.user_id,
                    idempotency_key=approval.idempotency_key or "",
                )
            )
            decided = approvals.decide(
                approval.id,
                workspace_id=identity.workspace_id,
                tenant_id=identity.tenant_id,
                status=ApprovalStatus.APPROVED,
                approver_user_id="approver",
                decided_at=now,
                token_hash="hash-only-token",
            )
            self.assertIsNotNone(decided)
            self.assertEqual(
                approvals.token_hash(
                    approval.id,
                    workspace_id=identity.workspace_id,
                    tenant_id=identity.tenant_id,
                ),
                "hash-only-token",
            )

            attachment = AttachmentRecord(
                id=f"attachment-{suffix}",
                workspace_id=identity.workspace_id,
                tenant_id=identity.tenant_id,
                user_id=identity.user_id,
                filename="evidence.png",
                content_type="image/png",
                image_format="PNG",
                size_bytes=4,
                sha256="synthetic-sha256",
                width=1,
                height=1,
                storage_path=f"{identity.workspace_id}/evidence.png",
                created_at=now,
            )
            attachments.create(attachment)
            self.assertIsNotNone(
                attachments.get(
                    attachment.id,
                    workspace_id=identity.workspace_id,
                    tenant_id=identity.tenant_id,
                    user_id=identity.user_id,
                )
            )
            self.assertIsNone(
                attachments.get(
                    attachment.id,
                    workspace_id="other-workspace",
                    tenant_id=identity.tenant_id,
                    user_id=identity.user_id,
                )
            )
            self.assertIsNotNone(
                attachments.delete(
                    attachment.id,
                    workspace_id=identity.workspace_id,
                    tenant_id=identity.tenant_id,
                    user_id=identity.user_id,
                )
            )
        finally:
            for store in (audit, runs, approvals, attachments):
                store.close()


if __name__ == "__main__":
    unittest.main()
