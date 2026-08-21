# Changelog

All notable changes are documented here.

## Unreleased

- Added tenant-scoped image evidence upload, validation, preview and audit.
- Added durable SQLite attachment metadata and restart-safe run records.
- Added signed HS256 JWT authentication boundary and production fail-closed
  auth-mode guard.
- Added the Web operations console and production-track release gates.
- Added the Agent Runtime order-status vertical slice.
- Added validated Agent, Skill, MCP, Plugin and schedule contracts.
- Added a review-gated local Plugin registry and deterministic scheduler.
- Added a FastAPI developer-preview API, SQLite audit adapter and local Docker
  packaging.
- Added a fail-closed production preflight and explicit opt-in live connector
  smoke for model, MCP, Vision/OCR, S3, ClamAV and OTLP boundaries.
- Added a Redis schedule producer with per-slot idempotency, continuous worker
  graceful shutdown and immutable production container dependencies.
- Added PostgreSQL shared-state adapters, checked-in PostgreSQL migrations,
  Compose migration gating and a password-safe custom-format backup helper;
  target backup/restore and failover evidence remain release gates.
- Added durable Redis cooperative cancellation for scheduled runs, including
  idempotent operator cancellation and cancellation checks before the next ERP
  or model operation.
- Hardened high-risk policy authorization so an arbitrary approval token cannot
  grant access; only a durable, scope- and argument-bound approval verifier may
  authorize a write-class tool.
- Made SQLite idempotent run persistence return the existing scoped record on a
  unique-key race, matching the shared PostgreSQL adapter's retry behavior.
- Added tenant-scoped audit reads, pinned PostgreSQL CI services, hosted
  PostgreSQL integration/backup recovery smoke and backup-wrapper tests.
- Improved the Web console's dynamic runtime counts, date/status copy and
  live-result accessibility states.

## 0.1.0-dev

Initial public developer-preview preparation. Real ERP, external model,
distributed worker, persistent multi-tenant deployment and hosted operations
are not included.
