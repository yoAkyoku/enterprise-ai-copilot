CREATE TABLE IF NOT EXISTS approval_requests (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    requester_user_id TEXT NOT NULL,
    requester_role TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    arguments_json TEXT NOT NULL,
    arguments_hash TEXT NOT NULL,
    risk TEXT NOT NULL,
    idempotency_key TEXT,
    requested_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    status TEXT NOT NULL,
    approver_user_id TEXT,
    decided_at TEXT,
    token_hash TEXT,
    UNIQUE(workspace_id, tenant_id, requester_user_id, idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_approval_scope
ON approval_requests(workspace_id, tenant_id, status, requested_at);
