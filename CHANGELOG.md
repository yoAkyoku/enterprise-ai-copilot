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

## 0.1.0-dev

Initial public developer-preview preparation. Real ERP, external model,
distributed worker, persistent multi-tenant deployment and hosted operations
are not included.
