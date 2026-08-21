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
- an OpenAI-compatible model endpoint if model explanations are enabled; it
  must be HTTPS, host-allowlisted and receive only consented, verified evidence;
- an S3-compatible bucket with server-side encryption and a lifecycle policy;
  configure an explicit HTTPS `AGENT_S3_ENDPOINT`, exact
  `AGENT_S3_ALLOWED_HOSTS` and `AGENT_S3_KMS_KEY_ID`;
- ClamAV or an equivalent reviewed scanner reachable through the clamd protocol;
- PostgreSQL for shared durable state, a persistent volume for its data, and
  `pg_dump`/`pg_restore` backup tooling with a tested restore;
- a persistent volume for `/app/.data` only when explicitly selecting the
  documented single-node SQLite mode;
- TLS termination, ingress authentication/rate limits and centralized logs.

Copy `deploy/production.env.example` into a secret-managed file, replace every
`example.invalid` host and `REPLACE_*` value, and inject secret values through
the deployment platform. Never commit the resulting file or credentials.

## Compose deployment

The checked-in `docker-compose.yml` is safe for local and small self-hosted
development deployments. Production uses the separate API/worker profile so a
long-running worker is not accidentally omitted:

```powershell
$env:AGENT_ENV_FILE = ".env.production"
docker compose --project-directory . -f deploy/docker-compose.production.yml config --quiet
docker compose --project-directory . -f deploy/docker-compose.production.yml up -d --build
```

The production profile runs the API, a migration job, a schedule producer, a
continuous Redis Streams worker, Redis and PostgreSQL. It binds the API to
loopback for a TLS reverse proxy, uses read-only container filesystems, and
restarts long-running services after failure. Replace the
example env file with a secret-managed file before starting; the checked-in
example intentionally points at `example.invalid` and is not a live deployment.

The checked-in production profile selects PostgreSQL and runs the checked-in
migrations before API and worker services. The PostgreSQL adapter provides the
shared metadata boundary needed by multiple API replicas, but horizontal
scaling, failover and row-level policy remain deployment validation gates.
SQLite remains available only when `AGENT_STORAGE_MODE=sqlite` is explicitly
selected; that mode is single-node and must not share its volume across API
replicas.

Place the API behind a TLS reverse proxy; do not expose the unauthenticated
development port directly to the public Internet. The production environment
must set `AGENT_PLATFORM_ENV=production`, `AGENT_PROVIDER_MODE=remote`,
`AGENT_AUTH_MODE=oidc_jwks` (or reviewed `jwt_hs256`), S3 storage, malware
scanning, positive retention, `AGENT_REDIS_URL` and
`AGENT_TRACE_ENDPOINT`/`AGENT_TRACE_ALLOWED_HOSTS`. Startup fails closed when
these requirements are missing. Production also requires the model endpoint,
API key, model name and exact model host allowlist; a model call still requires
per-request or per-schedule `allow_external_processing=true`.

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
not sufficient. Run the checked-in PostgreSQL migrations against the persistent
database before accepting traffic. The Compose migration service performs this
step; the standalone command is useful for controlled deployments:

```powershell
python scripts/migrate_postgres.py $env:AGENT_DATABASE_URL
Invoke-WebRequest http://127.0.0.1:8000/health
Invoke-WebRequest http://127.0.0.1:8000/ready
```

For an explicitly selected single-node SQLite deployment, use
`python scripts/migrate.py .data/agent-platform.sqlite3` instead.

Before `up`, run the static preflight using the secret-managed environment;
after dependencies are reachable, add `--live`. The live mode sends only
bounded read-oriented probes: authenticated HTTP reachability for MCP/model/
trace, OIDC JWKS reachability, Redis PING, S3 `HeadBucket` and a bounded ClamAV
INSTREAM smoke payload. It does not claim backup/restore, model correctness or
external business-data correctness:

```powershell
python -m scripts.production_preflight --json
python -m scripts.production_preflight --live
```

The live connector smoke is intentionally separate because it can make model
calls and creates one temporary encrypted object. Run it only from an
operator-controlled host with a disposable worker identity:

```powershell
python -m scripts.connector_smoke --confirm-live --only mcp
python -m scripts.connector_smoke --confirm-live --only all
```

Capture the JSON preflight output, smoke output, image digest, dependency
versions and target timestamps in `docs/validation/evidence-index.csv`. A
successful command with synthetic credentials is not production evidence.

For the complete target-environment acceptance, use the protected manual
GitHub workflow documented in [PRODUCTION_ACCEPTANCE.md](PRODUCTION_ACCEPTANCE.md).
It tests the deployed API with two real OIDC bearer tokens, verifies
workspace/tenant isolation for runs and attachments, and cleans up its
synthetic attachment fixtures. It is not an automatic deployment workflow and
must be approved through the configured GitHub Environment.

The API exposes authenticated `/metrics`. Forward only privacy-safe metrics and
OTLP spans to the monitoring system. Preserve `X-Request-Id`, trace, run,
workspace and audit identifiers in the log/trace pipeline without recording
tokens, image bytes or raw sensitive arguments.

## Scheduled worker

The `scheduler` service polls the reviewed schedule and uses a Redis
idempotency key for each due slot. Keep it enabled; starting only the worker
would consume jobs but would not produce future scheduled jobs. Run the worker
as a separate process with the same immutable image and
production environment. Production and staging require real Agent execution;
the dry-run mode is for local contract checks only:

```powershell
python -m services.worker.main schedules/order-status-demo.yaml `
  --redis-url $env:AGENT_REDIS_URL --queue-mode consume --continuous `
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

Model output is an optional explanation layer. The API and schedule contract
default to no external model processing. When consent is enabled, the runtime
still composes the verified ERP status itself and labels returned model prose
as `Model explanation (unverified)`. A provider failure produces a partial
success with the verified status preserved; it never becomes external-state
confirmation.

## Backup, rollback and release evidence

For the production Compose profile, use
`python scripts/backup_postgres.py <destination>` with `AGENT_DATABASE_URL`
from the secret manager. The helper creates a custom-format backup without
putting the password in the `pg_dump` argument list; restore it with
`pg_restore` into an isolated PostgreSQL instance, then run readiness and
scoped-data checks. Store backups outside the database volume and encrypt them.
For an explicitly selected single-node SQLite deployment, use
`scripts/backup_sqlite.py` instead. Roll back by deploying the previous
immutable image/tag, applying only backward-compatible migrations, and
recording the decision in the release evidence.

A release is not production-ready until the corresponding rows in
`docs/validation/evidence-index.csv` are `PASS` or explicitly `WAIVED` with
owner, expiry, risk and compensating control. The release gate also verifies
that every evidence commit exists and is reachable from the release commit.
In particular, local synthetic tests do not prove OIDC, ERP/MCP, S3, ClamAV,
Redis worker, Docker, browser or hosted deployment behavior.
