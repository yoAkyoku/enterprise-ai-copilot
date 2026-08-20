# Security Policy

This project is on a production-track path but is not approved for production
credentials or sensitive customer data until the current release gates are
completed and recorded. Unrestricted network access is never a default.

## Reporting a vulnerability

Please do not open a public issue for an exploitable vulnerability. Contact the
maintainers through the private security reporting channel configured for the
GitHub repository and include a minimal reproduction, affected version and
impact. Redact credentials, tokens, personal data and private infrastructure
details from the report.

If no private channel is configured yet, open a GitHub security advisory draft
or contact the repository owner privately before public disclosure.

## Current security boundary

The current local track demonstrates fail-closed identity and tenant checks,
signed HS256 JWT authentication, tool risk classification, provenance checks,
durable SQLite audit/run/attachment metadata, image content validation and
review-gated Plugin installation. Real MCP credentials, provider egress,
OIDC/JWKS rotation, malware scanning, object-storage isolation, distributed
worker durability, sandbox isolation, dependency/history scanning and hosted
deployment remain out of scope until their validation gates are recorded as
`PASS`.
