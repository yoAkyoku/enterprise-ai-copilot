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
4. The preview SQLite adapter is single-process durability, not a clustered
   audit guarantee.

## Primary threats and controls

| Threat | Control | Evidence | Remaining limit |
|---|---|---|---|
| Cross-tenant read | Identity-injected gateway lookup and workspace/tenant-bound API run access | `SEC-007`, `MCP-007` | Real connector query review not run |
| Header identity spoofing | Bearer mode is the API default; header mode is test-only | API bearer test | External IdP integration not included |
| Unauthorized tool use | Role allowlist and fail-closed policy | `POL-001`, `POL-002`, API blocked test | Agent-specific permission import is pending |
| Plugin path escape | Containment, symlink rejection, review-gated install | Plugin contract tests | Signature verification pending |
| False success | Connector failure and provenance verification before success | `OBS-002`, `MCP-008` | Distributed retry semantics pending |
| Secret leakage | `.env` ignore, safe defaults and source scan | local secret scan | History and dependency scanners pending |
| Malicious image upload | Content sniffing with Pillow, allowlisted formats, byte/pixel limits, safe storage path, pre-storage scanner boundary and `nosniff` responses | `ATT-001`, `ATT-002`, `IMG-005` | A production deployment must configure the included ClamAV adapter or an equivalent reviewed scanner |
| Approval tampering | Workspace/tenant scope, expiry, canonical argument hash, atomic status transition and hash-only one-time token | `POL-011` | External write execution must consume and verify the token at the connector boundary |
| Object-storage exposure | Generated key containment, exact endpoint allowlist, bounded reads and server-side encryption request | `IMG-008` | Real bucket policy, backup and restore evidence remain deployment-specific |
| Provider/MCP SSRF | HTTPS exact-host allowlist, literal private/loopback/metadata host rejection, no redirects and bounded responses | `MCP-011`, `IMG-006` | DNS rebinding and target proxy policy require deployment-level controls |
| Cross-scope attachment read | Metadata and content queries require authenticated user/workspace/tenant scope | `ATT-001`, `ATT-002` | Admin/support scoped access policy pending |
| Token claim forgery | HS256 signature, expiry, issuer/audience options and required identity claims; coordinated HS256 rotation runbook | `AUTH-001`, `AUTH-002` | A real deployment must execute and record the selected OIDC or HS256 process |
| Durable run leakage | SQLite run reads filter user, workspace, tenant and role; audit dashboard filters workspace | `RUN-001` | Clustered database and row-level policy validation pending |

## Explicit non-goals for v0.1

The current production-track branch does not claim protection against
compromised hosts, a malicious maintainer, a compromised external identity
provider, distributed-worker race conditions, malware scanning, or full
prompt-injection resistance for arbitrary RAG documents. These are release
gates, not implicit guarantees.
