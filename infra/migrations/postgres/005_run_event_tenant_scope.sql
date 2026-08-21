ALTER TABLE run_events ADD COLUMN IF NOT EXISTS tenant_id TEXT;

UPDATE run_events
SET tenant_id = payload_json::jsonb ->> 'tenant_id'
WHERE tenant_id IS NULL;

CREATE INDEX IF NOT EXISTS idx_run_events_scope
ON run_events(workspace_id, tenant_id, sequence);
