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
- an OTLP/HTTP trace collector with an HTTPS allowlisted endpoint;
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
scanning, positive retention, `AGENT_REDIS_URL` and
`AGENT_TRACE_ENDPOINT`/`AGENT_TRACE_ALLOWED_HOSTS`. Startup fails closed when
these requirements are missing.

### HS256 rotation when OIDC is unavailable

OIDC/JWKS is preferred. If a self-hosted operator uses `jwt_hs256`, the
single-secret adapter requires a coordinated rotation because it deliberately
does not accept two signing keys at once:

1. Store the current and next 32-byte-or-longer secrets as versioned secret
   references, with issuer and audience fixed in configuration. Keep access to
   the old version only for the rollback window; never put either value in a
   file, URL, log or repository.
2. Set a short token lifetime and announce a maintenance window. Stop issuing
   new tokens, drain API and worker traffic, and put the deployment in a
   controlled reauthentication state. Do not perform a rolling mixed-secret
   deploy, because it would make authentication nondeterministic across
   replicas.
3. Replace `AGENT_JWT_SECRET` in the secret manager and restart every API and
   worker process from the same immutable image. Verify that a newly issued
   token succeeds and a token signed with the old secret is rejected, while
   issuer, audience, expiry and workspace/tenant/role claims remain enforced.
4. Re-enable traffic only after `/ready`, authenticated API smoke, audit trace
   continuity and queue health pass. Revoke/delete the old secret after the
   token lifetime plus the documented rollback window.
5. If verification fails, stop traffic and restore the previous secret version
   and immutable image as one operation, then record the decision in the
   incident/release evidence.

This process is an operational control, not proof that a particular deployment
has rotated its key. Record the operator, secret versions, timestamps and
verification artifact for each real rotation.

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

The API exposes authenticated `/metrics`. Forward only privacy-safe metrics and
OTLP spans to the monitoring system. Preserve `X-Request-Id`, trace, run,
workspace and audit identifiers in the log/trace pipeline without recording
tokens, image bytes or raw sensitive arguments.

## Scheduled worker

Run the worker as a separate process with the same immutable image and
production environment. Production and staging require real Agent execution;
the dry-run mode is for local contract checks only:

```powershell
python -m services.worker.main schedules/order-status-demo.yaml `
  --redis-url $env:AGENT_REDIS_URL --queue-mode consume `
  --execution-mode agent --worker-id worker-01
```

`AGENT_WORKER_USER_ID`, `AGENT_WORKER_WORKSPACE_ID`,
`AGENT_WORKER_TENANT_ID` and `AGENT_WORKER_ROLE` come from deployment
configuration, never from Redis job payloads. The worker verifies schedule ID,
version, task inputs and exact payload keys before execution, passes the
trusted identity through the same policy/MCP boundary as the API, and exports
the same privacy-safe OTLP tool spans. Keep the Redis consumer group and
`claim_after_seconds` longer than the maximum expected task runtime; verify
reclaim and cancellation behavior in the target deployment.

## Backup, rollback and release evidence

Use `scripts/backup_sqlite.py` to create and integrity-check a database backup.
Store backups outside the application volume, encrypt them, and periodically
perform a restore into an isolated environment. Roll back by deploying the
previous immutable image/tag, applying only backward-compatible migrations,
and recording the decision in the release evidence.

A release is not production-ready until the corresponding rows in
`docs/validation/evidence-index.csv` are `PASS` or explicitly `WAIVED` with
owner, expiry, risk and compensating control. The release gate also verifies
that every evidence commit exists and is reachable from the release commit.
In particular, local synthetic tests do not prove OIDC, ERP/MCP, S3, ClamAV,
Redis worker, Docker, browser or hosted deployment behavior.
