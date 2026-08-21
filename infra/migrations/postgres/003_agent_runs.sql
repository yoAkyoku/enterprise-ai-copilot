CREATE TABLE IF NOT EXISTS agent_runs (
    run_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    trace_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    message TEXT NOT NULL,
    source_id TEXT,
    observed_at TEXT,
    external_ref TEXT,
    user_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    role TEXT NOT NULL,
    idempotency_key TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(workspace_id, tenant_id, user_id, role, idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_agent_runs_scope
ON agent_runs(workspace_id, tenant_id, user_id, created_at);
