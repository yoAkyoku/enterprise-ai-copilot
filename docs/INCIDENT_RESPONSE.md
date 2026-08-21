# Incident response runbook

This is the minimum response procedure for a self-hosted deployment. The
operator must add named responders, escalation contacts and notification
deadlines before production use.

## Severity and first response

- **P0**: suspected credential exposure, cross-tenant access, uncontrolled
  external write or public attachment disclosure. Page the on-call responder,
  stop external writes and isolate the affected deployment immediately.
- **P1**: repeated provider compromise indicators, queue corruption, malware
  scanner bypass or material availability loss. Freeze risky workflows and
  begin containment within the operator's documented SLA.
- **P2**: bounded single-request failure or non-sensitive observability issue.
  Track, remediate and include it in the next release review.

## Containment

1. Preserve the request, trace, run, audit and provider correlation IDs without
   copying secrets or raw sensitive payloads.
2. Disable affected Agent schedules and external-send/write tools through
   policy or configuration. Do not delete the evidence database before a
   forensic backup is made.
3. Revoke and replace identity, MCP, S3, Vision/OCR, OTLP and Redis credentials
   that may have been exposed. Remove the old secret from the deployment
   secret manager after confirming the replacement is active.
4. Isolate the affected workspace/tenant and inspect audit events for adjacent
   scopes. Never use a model-generated claim to decide the affected scope.
5. If attachments or external processing are involved, disable publication or
   provider egress, record affected object keys and request provider deletion.

## Recovery and notification

Validate the patched immutable image, migrations, provider allowlists, scanner,
backups and readiness checks in an isolated environment before restoring
traffic. Reconcile queue jobs by idempotency key and do not report an external
write as successful without a provider receipt. Notify customers, regulators
and processors according to the operator's legal and contractual obligations.

## Closure

Record timeline, root cause, affected scopes, evidence locations, credentials
rotated, customer notifications, recovery validation and corrective actions.
Add a regression test or policy check for every preventable technical cause.
