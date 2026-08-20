# Production deployment runbook

This runbook describes the boundary for a self-hosted production deployment.
It is a deployment procedure and configuration contract, not evidence that a
production environment has already been provisioned. The current repository
contains deterministic local evidence; external provider, infrastructure and
browser evidence must be recorded in the release evidence index.

## Required external dependencies

Before starting the API, provision and test:

- an OIDC provider with workspace, tenant and role claims, or a reviewed HS256
  key-rotation process;
- a reviewed remote MCP/ERP connector that enforces trusted identity headers
  and returns provenance (`source_id`, `observed_at`, `external_ref`);
- Redis for distributed rate limits and scheduled worker queue delivery;
- an S3-compatible bucket with server-side encryption and a lifecycle policy;
- ClamAV or an equivalent reviewed scanner reachable through the clamd protocol;
- a persistent volume for `/app/.data`, encrypted backups and a tested restore;
- TLS termination, ingress authentication/rate limits and centralized logs.

Copy `deploy/production.env.example` into a secret-managed file, replace every
`example.invalid` host and `REPLACE_*` value, and inject secret values through
the deployment platform. Never commit the resulting file or credentials.

## Compose deployment

The checked-in `docker-compose.yml` is safe for local and small self-hosted
deployments. It uses the development `.env.example` by default. To select a
production environment file, export the file path before starting the stack:

```powershell
$env:AGENT_ENV_FILE = ".env.production"
docker compose config --quiet
docker compose up -d --build
```

Place the API behind a TLS reverse proxy; do not expose the unauthenticated
development port directly to the public Internet. The production environment
must set `AGENT_PLATFORM_ENV=production`, `AGENT_PROVIDER_MODE=remote`,
`AGENT_AUTH_MODE=oidc_jwks` (or reviewed `jwt_hs256`), S3 storage, malware
scanning, positive retention and `AGENT_REDIS_URL`. Startup fails closed when
these requirements are missing.

## Readiness and migration

After the service starts, check `/health` for configured adapter identities and
`/ready` for the production dependency gate. A `200` from `/health` alone is
not sufficient. Run the checked-in migrations against the persistent database
before accepting traffic:

```powershell
python scripts/migrate.py .data/agent-platform.sqlite3
Invoke-WebRequest http://127.0.0.1:8000/health
Invoke-WebRequest http://127.0.0.1:8000/ready
```

The API exposes authenticated `/metrics`. Forward only privacy-safe metrics to
the monitoring system. Preserve `X-Request-Id`, trace, run, workspace and
audit identifiers in the log/trace pipeline without recording tokens, image
bytes or raw sensitive arguments.

## Backup, rollback and release evidence

Use `scripts/backup_sqlite.py` to create and integrity-check a database backup.
Store backups outside the application volume, encrypt them, and periodically
perform a restore into an isolated environment. Roll back by deploying the
previous immutable image/tag, applying only backward-compatible migrations,
and recording the decision in the release evidence.

A release is not production-ready until the corresponding rows in
`docs/validation/evidence-index.csv` are `PASS` or explicitly `WAIVED` with
owner, expiry, risk and compensating control. In particular, local synthetic
tests do not prove OIDC, ERP/MCP, S3, ClamAV, Redis worker, Docker, browser or
hosted deployment behavior.
