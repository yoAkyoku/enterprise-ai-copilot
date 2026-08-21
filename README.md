# Enterprise AI Copilot

An open-source, self-hostable Enterprise Agent Operating Platform.

The repository is on the production-track path toward a formal self-hosted
release. It currently provides a policy-checked Customer Service Agent, a
tenant-scoped operations console, durable run/audit/approval metadata, bounded
image evidence storage, Vision/OCR and remote MCP boundaries, Redis queue/rate
limit adapters, authenticated metrics and a replaceable OTLP trace exporter.
The default demo still uses a fake
ERP MCP tool and synthetic data so the public test suite never needs private
services.

## Run the first slice

```powershell
python -m pip install -e .
python -m unittest discover -s tests -v
python -m services.api.main --order-id SO-1001
python -m services.cli validate
python -m services.cli doctor
enterprise-agent --serve
```

The web console is available at `http://127.0.0.1:8000/` after starting the
server. In local bearer mode, enter the value of `AGENT_API_TOKEN` in the
console. Production and staging configuration fail closed unless
`AGENT_AUTH_MODE=jwt_hs256` is used with a secret of at least 32 bytes, or
`AGENT_AUTH_MODE=oidc_jwks` is configured with an HTTPS issuer, exact host
allowlist and trusted workspace/tenant/role claims.

The demo uses synthetic ERP data. A self-hosted deployment can replace the
runtime, audit, run, attachment and MCP adapters without granting agents
direct database, filesystem or network access.

## Implemented image evidence flow

The operations console and `/api/v1/attachments` API support JPEG, PNG, WebP
and GIF uploads. The server validates the actual image bytes, declared media
type, file size, dimensions and safe filename; stores content below a
tenant/workspace-derived path; records a SHA-256 digest and dimensions in
SQLite; and emits append-only create/delete audit events. Metadata and content
reads are scoped to the authenticated user, workspace and tenant.

The repository also includes an OpenAI-compatible Vision/OCR adapter with
HTTPS host allowlisting, timeout and explicit external-processing consent. It
is disabled until endpoint, key, model and allowed-host configuration are
provided. Uploads can use the included ClamAV `clamd` INSTREAM adapter; in
staging/production startup, malware scanning and a positive retention period
are required. Object storage and backup remain production connector gates; they
are not silently claimed as executed in the synthetic release.

The text-model boundary uses the same OpenAI-compatible contract. It receives
only a user query plus server-verified evidence, requires explicit
`allow_external_processing` consent, and labels returned prose as unverified.
High-risk tool authorization is also fail-closed: an approval token is not
accepted as proof by itself. A configured runtime must bind policy to the
durable, workspace/tenant- and argument-scoped approval verifier before a
write-class tool can run.
Registered tools can also be invoked through
`POST /api/v1/tools/{tool_name}/execute` with typed arguments and an optional
one-time approval id/token for read operations; write-class operations require
an argument-bound approval and explicit idempotency key. The runtime validates the Agent manifest allowlist,
tool risk, argument schema, tenant/workspace provenance and connector
idempotency before returning a success. The example ERP boundary includes a
review-gated `erp.create_return` action; the default data is still synthetic.
Production Plugin registries can require Ed25519 signatures against
deployment-provided trusted publisher keys; the local demo registry remains
review-gated and never executes Plugin code.
The production Compose profile starts the API, a PostgreSQL migration job, a
Redis-backed schedule producer, a continuous worker, Redis and PostgreSQL;
replace the example environment with secret-managed values before use.
Run `python -m scripts.production_preflight --json` before startup, then use
`--live` after dependencies are reachable to probe Redis, authenticated MCP,
OIDC, model, trace, S3 and ClamAV boundaries. The checked-in production profile
uses PostgreSQL for shared metadata. SQLite is still available only as an
explicitly selected single-node deployment; do not share its volume across API
replicas.

Release tags are checked against the package version and must use a stable
`vMAJOR.MINOR.PATCH` value; development versions such as `0.2.0.dev0` cannot
publish through the release workflow.

Multi-replica staging/production also requires `AGENT_REDIS_URL`; the API uses
an atomic Redis limiter for upload and Vision/OCR abuse controls. Development
without Redis intentionally uses an in-process limiter.

Scheduled production workers run `services.worker.main` in `agent` mode with a
deployment-controlled service identity. Queue payloads cannot choose tenant or
user scope; the worker revalidates the reviewed schedule before invoking the
same Agent Runtime and MCP policy boundary. Operators can place a short-lived,
idempotent Redis cancellation marker with
`python -m scripts.cancel_schedule_run --confirm-cancel`; the worker then
stops queued/retrying work and checks cancellation before the next ERP or model
operation. Cancellation is cooperative and cannot undo an external call that
was already in flight.
The current scheduled Agent executor is read-only; `approved_write` schedules
fail closed until a dedicated approval-bound schedule action executor is
implemented and reviewed.

Production also requires `AGENT_TRACE_ENDPOINT` and an exact
`AGENT_TRACE_ALLOWED_HOSTS` entry. Trace attributes are bounded and reject
credential, token, prompt, image and raw-content fields before export.

Production attachment bytes use the S3-compatible adapter (`AGENT_ATTACHMENT_STORAGE=s3`)
with server-side encryption, exact endpoint allowlisting when a custom endpoint
is used, and bounded reads. Local development defaults to the contained
filesystem adapter.

## Design and validation

- [Software Design Document](docs/SDD.md)
- [Validation Standard](docs/VALIDATION_STANDARD.md)
- [Agent instructions](AGENTS.md)
- [Contributing guide](CONTRIBUTING.md)
- [Security policy](SECURITY.md)
- [Apache-2.0 License](LICENSE)
- [Threat model](docs/THREAT_MODEL.md)
- [Privacy and retention policy](docs/PRIVACY_RETENTION.md)
- [Incident response runbook](docs/INCIDENT_RESPONSE.md)
- [Migration and rollback procedure](docs/MIGRATION_ROLLBACK.md)
- [Production deployment runbook](docs/DEPLOYMENT.md)
- [Release checklist](docs/release/v0.1.0-dev.md)
- [Production-track checklist](docs/release/v0.2.0-production-track.md)
- [Protected production acceptance](docs/PRODUCTION_ACCEPTANCE.md)
- [Validation evidence](docs/validation/evidence-index.csv)

## Status

This is not yet a formal production release. Local scheduler/queue adapters,
PostgreSQL and SQLite audit/run/approval adapters, manifest validator,
review-gated Plugin registry, HTTP API, operations console, image evidence
flow and metrics are included. Formal release still requires target-environment
identity/provider, connector, S3/ClamAV/Redis, PostgreSQL backup/restore,
Docker/browser, CI/tagged-release and external deployment evidence.

## API authentication boundary

The HTTP API defaults to Bearer authentication. Set `AGENT_API_TOKEN` to a
random local value before `enterprise-agent --serve`, then send
`Authorization: Bearer <token>`. The test-only
`headers` mode accepts `X-User-Id`, `X-Workspace-Id`, `X-Tenant-Id` and `X-Role`
and must never be exposed directly on the Internet. OIDC deployments use the
RS256 JWKS adapter and require `cryptography` from the `production` extra.
