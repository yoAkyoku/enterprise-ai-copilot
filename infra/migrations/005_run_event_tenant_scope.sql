ALTER TABLE run_events ADD COLUMN tenant_id TEXT;

UPDATE run_events
SET tenant_id = json_extract(payload_json, '$.tenant_id')
WHERE tenant_id IS NULL
  AND json_type(payload_json, '$.tenant_id') = 'text'
  AND trim(json_extract(payload_json, '$.tenant_id')) <> '';

CREATE INDEX IF NOT EXISTS idx_run_events_scope
ON run_events(workspace_id, tenant_id, sequence);
