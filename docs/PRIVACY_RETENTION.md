# Privacy and retention policy

This policy applies to the self-hosted Enterprise AI Copilot reference
deployment. The operator is the data controller for the deployment and must
adapt retention periods, notices and deletion procedures to local law and
customer contracts.

## Data classes

- Identity claims: subject, workspace, tenant and role claims required for an
  authenticated request. Claims are used for authorization and are not copied
  into model prompts or trace attributes.
- Operational records: request, trace, run, tool, approval and audit metadata.
  These records retain identifiers, status, provenance and policy decisions,
  but must not contain bearer tokens, secret values, image bytes or raw prompts.
- Attachments: validated image bytes and metadata. Bytes are tenant/workspace
  scoped, malware-scanned before publication in production, encrypted by the
  configured object store and deleted according to the configured retention
  period and explicit user deletion operation.
- External-processing data: image bytes sent to Vision/OCR only after the API
  request explicitly grants external processing consent. Operators must use a
  provider contract and endpoint allowlist that covers deletion, training use,
  region and subprocessor requirements.

## Defaults and controls

- Development fixtures contain synthetic identities, orders and images only.
- Production requires positive attachment retention, encrypted S3-compatible
  storage, malware scanning and distributed rate limiting.
- Production traces and metrics are allowlisted and bounded. Sensitive keys
  containing credentials, tokens, prompts, images or raw content are rejected.
- SQLite audit/run metadata and backups must be encrypted at rest by the host
  or storage service. Backups must use a shorter access list than application
  data and must have a documented expiry.
- Operators must configure a retention value for audit/run records separately
  from attachment retention and document the value in the deployment record.

## Deletion and access

Deletion requests must identify the authenticated workspace and tenant; an
operator must not use a client-supplied tenant identifier to broaden scope.
Delete attachment bytes and metadata together, retain only the minimum audit
event needed to prove the deletion, and propagate deletion to encrypted
backups according to the backup expiry schedule. Support access must be
time-bound, approved and audited.

## Review checklist

Before a production launch, the operator records the jurisdiction, data
processor contracts, configured retention values, backup expiry, Vision/OCR
consent wording, deletion test result and incident contact. A release is not
evidence of compliance merely because this policy exists.
