# Contributing

Thank you for helping build the Enterprise Agent Operating Platform.

## Development setup

The developer preview uses a small, declared Python dependency set and no
external service. From a clean checkout with Python 3.12 or newer:

```text
python -m pip install -e ".[dev]"
python -m compileall -q packages services tests scripts apps
ruff check packages services tests scripts
python -m unittest discover -s tests -v
python -m services.cli validate
python -m services.api.main --order-id SO-1001
python -m pip_audit
```

All demo data is synthetic. Do not add credentials, customer data, production
URLs or provider payloads to tests, fixtures, documentation or issue reports.

## Change expectations

- Read `AGENTS.md`, `docs/SDD.md` and `docs/VALIDATION_STANDARD.md` before making
  architectural changes.
- Keep tool calls typed, tenant-scoped, policy-checked and auditable.
- Add deterministic tests for every changed policy, manifest, MCP contract or
  runtime state transition.
- Mark unrun validation as `NOT_RUN` or `UNVERIFIED`; do not infer release
  readiness from a local unit-test pass.
- Keep commits focused and explain security or compatibility trade-offs.

## Pull requests

Pull requests should describe the user-visible behavior, affected boundaries,
tests executed, known limitations and any required migration or configuration
change. Security vulnerabilities must follow `SECURITY.md` instead of being
posted publicly.
