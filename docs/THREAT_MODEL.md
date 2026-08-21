# Production-track threat model

Review status: repository review completed 2026-08-21. This threat model is
paired with [Privacy and retention](PRIVACY_RETENTION.md), [Incident response](INCIDENT_RESPONSE.md)
and [Migration and rollback](MIGRATION_ROLLBACK.md); target-environment
controls remain deployment-specific gates.

## Assets

- Tenant and workspace boundaries.
- Agent instructions, Skill procedures and Plugin manifests.
- Tool credentials and external-system state.
- Run events, provenance and user-visible responses.
- Uploaded image content, attachment metadata, thumbnails and provider egress.

## Trust boundaries

1. The authenticated identity provider is outside this repository. Production
   and staging require a signed JWT boundary; local preview may use a shared
   Bearer token, and the API never treats user text as identity.
2. Agent instructions, Skills, Plugins and retrieved documents are untrusted
   content. Runtime policy remains authoritative.
3. MCP tools are external capability boundaries. Only registered, allowlisted,
   risk-classified tools may execute.
4. PostgreSQL is the shared production metadata boundary; SQLite remains a
   single-process/single-node option and is not a clustered audit guarantee.

## Primary threats and controls

| Threat | Control | Evidence | Remaining limit |
|---|---|---|---|
| Cross-tenant read | Identity-injected gateway lookup and workspace/tenant-bound API run access | `SEC-007`, `MCP-007` | Real connector query review not run |
| Header identity spoofing | Bearer mode is the API default; header mode is test-only | API bearer test | External IdP integration not included |
| Unauthorized tool use | Role allowlist, Agent manifest allowlist, typed argument validation and fail-closed policy | `POL-001`, `POL-002`, generic tool execution tests | Real connector authorization and deployment role mapping remain external gates |
| Plugin path escape and package substitution | Containment, symlink rejection, review-gated install, integrity record and optional trusted Ed25519 publisher signature | Plugin contract tests | Production key distribution/rotation and clean-environment install evidence remain deployment gates |
| False success | Connector failure and provenance verification before success | `OBS-002`, `MCP-008` | Distributed retry semantics pending |
| Secret leakage | `.env` ignore, safe defaults and source scan | local secret scan | History and dependency scanners pending |
| Malicious image upload | Content sniffing with Pillow, allowlisted formats, byte/pixel limits, safe storage path, pre-storage scanner boundary and `nosniff` responses | `ATT-001`, `ATT-002`, `IMG-005` | A production deployment must configure the included ClamAV adapter or an equivalent reviewed scanner |
| Approval tampering | Workspace/tenant/user scope, expiry, canonical argument hash, atomic status transition and hash-only one-time token | `POL-011`, generic tool execution tests | External connector must enforce its own authorization and idempotency contract |
| Unattended scheduled write | Scheduler blocks unapproved writes and the Agent worker rejects `approved_write` before runtime execution | `SCH-008`, worker contract tests | A dedicated schedule action executor with a user-bound approval grant is not included |
| Object-storage exposure | Generated key containment, exact endpoint allowlist, bounded reads, server-side encryption request and post-write metadata verification | `IMG-008` | Real bucket policy, backup and restore evidence remain deployment-specific |
| Provider/MCP SSRF | HTTPS exact-host allowlist, literal private/loopback/metadata host rejection, no redirects and bounded responses | `MCP-011`, `IMG-006` | DNS rebinding and target proxy policy require deployment-level controls |
| Cross-scope attachment read | Metadata and content queries require authenticated user/workspace/tenant scope | `ATT-001`, `ATT-002` | Admin/support scoped access policy pending |
| Token claim forgery | HS256 signature, expiry, issuer/audience options and required identity claims; coordinated HS256 rotation runbook | `AUTH-001`, `AUTH-002` | A real deployment must execute and record the selected OIDC or HS256 process |
| Durable run leakage | PostgreSQL and SQLite run reads filter user, workspace, tenant and role; audit dashboard and run-event reads filter workspace and tenant | `RUN-001`, `SEC-015` | Clustered database and row-level policy validation pending |

## Explicit non-goals for v0.1

The current production-track branch does not claim protection against
compromised hosts, a malicious maintainer, a compromised external identity
provider, distributed-worker race conditions, malware scanning, or full
prompt-injection resistance for arbitrary RAG documents. These are release
gates, not implicit guarantees.
