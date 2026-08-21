# Protected production acceptance

The repository does not claim that a target deployment exists. The protected
manual workflow `.github/workflows/production-acceptance.yml` provides the
repeatable evidence path once an operator has provisioned a staging or
production environment.

## Safety boundary

- The workflow runs only through `workflow_dispatch` and must be attached to a
  protected GitHub Environment (`staging` or `production`). Configure required
  reviewers and branch restrictions in GitHub before adding real credentials.
- It has `contents: read` only. It does not deploy, migrate, or change GitHub
  repository state.
- `connector_smoke.py` makes explicit provider calls and creates one temporary
  encrypted object. `api_acceptance_smoke.py` performs one safe ERP read and
  creates two synthetic 1x1 PNG attachments, then deletes them before it
  returns. Both commands require `--confirm-live`.
- `postgres_backup_restore_smoke.py` creates a custom-format backup in a
  temporary runner directory, restores it into `AGENT_RESTORE_DATABASE_URL`,
  verifies the checked-in migration table, and removes the dump before the
  workflow ends. The restore DSN must identify a separate, pre-provisioned
  empty database; the dump is never uploaded as an artifact.
- `redis_worker_smoke.py` uses a unique stream and consumer group to verify
  target Redis reconnect and abandoned-job reclaim. It deletes the synthetic
  stream in `finally`; durable cancellation and retry evidence still require
  the target worker/API acceptance path.
- Logs and artifacts contain statuses, HTTP codes and safe check names only;
  tokens, provider response bodies, image bytes and full URLs are not printed.
- A successful workflow is evidence for the exact checked-out commit and
  target environment only. It is not evidence for another environment or
  another image digest.

## Required environment secrets

Configure these as environment-scoped GitHub Secrets. Do not put them in the
repository, workflow YAML, an issue, or a committed `.env` file.

The provider and deployment values correspond to
`deploy/production.env.example`: database, OIDC issuer/audience/JWKS and host
allowlist, remote MCP/ERP endpoint/token, model endpoint/key/name/allowlist,
Vision/OCR endpoint/key/model/allowlist, Redis URL, worker identity, OTLP
endpoint/token/allowlist, ClamAV host, S3 bucket/region/endpoint/KMS key and
temporary S3 credentials, plus `AGENT_RESTORE_DATABASE_URL` for an isolated
empty PostgreSQL restore database.

The API acceptance probe additionally requires:

- `AGENT_ACCEPTANCE_BASE_URL` and `AGENT_ACCEPTANCE_ALLOWED_HOSTS`;
- two different short-lived bearer tokens and their expected
  `workspace_id`/`tenant_id` values:
  `AGENT_ACCEPTANCE_A_BEARER_TOKEN`, `AGENT_ACCEPTANCE_A_WORKSPACE_ID`,
  `AGENT_ACCEPTANCE_A_TENANT_ID`, and the corresponding `B` values;
- `AGENT_ACCEPTANCE_ORDER_ID`, a reviewed non-customer test order visible only
  to principal A's target scope.

Tokens must be issued by the configured OIDC provider and contain the same
claims that production uses. The expected scope values are assertions for the
acceptance probe; they do not become request headers and cannot select runtime
identity.

## Run and record evidence

1. Provision the target deployment using [the deployment runbook](DEPLOYMENT.md).
2. Confirm TLS, ingress authentication, database migration, backups, lifecycle
   policy, scanner policy and trace retention with the operator responsible for
   that environment.
3. In GitHub, select **Actions → Production acceptance (manual) → Run
   workflow**, choose the protected environment, and approve the environment
   review if required.
4. Download the generated artifact and record the workflow URL, run ID, exact
   commit SHA, container image digest, target environment, provider versions,
   timestamps and operator in `docs/validation/evidence-index.csv`.
5. Only after all required release checklist rows are `PASS` or a documented,
   non-prohibited `WAIVED` entry should a maintainer bump the stable package
   version, create the matching tag and publish the GitHub release.

The workflow is intentionally not a default CI job: it can incur provider
charges, touches real infrastructure and requires credentials that must never
be available to pull requests or fork builds.
