# Enterprise Agent Operating Platform

## Validation Standard

- Status: Production-track draft 0.2
- Applies to: source code, Agent manifests, Skills, Plugins, MCP servers, schedules, deployments and releases
- Current evidence status: partial local evidence; see `docs/validation/evidence-index.csv`. This does not claim production or external readiness.

## 1. Purpose

本文件定義如何判定本專案「可執行、可擴充、可安全開源」。所有結果必須以可重現的 command、commit、環境、輸出與 artifact 為證據。

## 2. Normative status vocabulary

| Status | Meaning |
|---|---|
| `PASS` | 已在指定 scope 執行，結果符合標準，且有可追溯證據 |
| `FAIL` | 已執行但至少一項必要條件未通過 |
| `UNVERIFIED` | 有部分線索，但未完成指定驗證或缺少足夠證據 |
| `MISSING` | 必要檔案、測試、設定或證據不存在 |
| `NOT_RUN` | 已定義但本次尚未執行 |
| `NOT_APPLICABLE` | 經維護者明確記錄理由後，確認不適用 |
| `WAIVED` | 經指定 maintainer 審批，包含期限、風險與替代措施 |

`PASS` 不得由文件存在、lint 通過、mock 成功或模型回答看起來合理推論而來。

## 3. Evidence levels

| Level | Scope | Acceptable evidence |
|---|---|---|
| E0 | Design | SDD、schema、threat model、decision record |
| E1 | Static | lint、typecheck、manifest validator、secret scan |
| E2 | Unit | deterministic unit tests with fixtures |
| E3 | Integration | real local DB、queue、MCP contract、fake external service |
| E4 | E2E | Docker stack through user-visible API/UI flow |
| E5 | Security | CodeQL、dependency scan、prompt injection、sandbox tests |
| E6 | Release | clean checkout, release artifact, install-from-artifact and checksum |
| E7 | External | real external provider, hosted deployment or production verification |

本專案開源 `v0.x` 至少需要 E0-E6；E7 若未執行，必須明確標示 `UNVERIFIED`，不得宣稱 production-ready。

## 4. Evidence record format

每個 test/check MUST 產生一筆 record：

```yaml
id: MCP-003
status: PASS
scope: local-docker
commit: <full-sha>
started_at: 2026-08-19T09:00:00+08:00
command: pytest tests/integration/test_mcp_gateway.py -q
exit_code: 0
environment:
  os: windows
  runtime: docker-compose
artifacts:
  - artifacts/validation/MCP-003.junit.xml
notes: Tool allowlist rejects an unapproved write tool.
```

Evidence MUST NOT include secrets, raw credentials, customer data or unredacted model payloads。

## 5. Test environment

Validation MUST document：

- OS and architecture
- runtime versions
- model provider mode (`fake`、`local`、`remote`)
- database version
- queue/worker mode
- feature flags
- network access mode
- seed dataset version
- commit SHA

Default CI MUST use fake providers and deterministic fixtures. Live provider tests MUST be opt-in and MUST never run with production credentials。The repository MUST pin formatter/linter versions so a local PASS and hosted PASS use the same toolchain.

## 6. Required validation matrix

### 6.1 Repository and installation

| ID | Check | Required result |
|---|---|---|
| INST-001 | clean checkout can install | `PASS` on supported OS matrix |
| INST-002 | `.env.example` is complete | no required secret is undocumented |
| INST-003 | Docker Compose starts | all health checks become ready |
| INST-004 | database migration | empty DB migrates without manual SQL |
| INST-005 | seed data | demo data loads and is clearly synthetic |
| INST-006 | first run | new user can complete one demo flow |
| INST-007 | doctor command | missing dependencies are actionable and non-secret |
| INST-008 | clean shutdown/restart | state is recoverable after restart |
| INST-009 | backup/restore | checked-in backup utility creates and verifies a restorable database artifact |
| INST-010 | shared PostgreSQL store | migrations, scoped stores, idempotency and health probes pass against a disposable PostgreSQL service |

### 6.2 Configuration and manifests

| ID | Check | Required result |
|---|---|---|
| CONF-001 | agent manifest schema | invalid fields fail closed |
| CONF-002 | `AGENT.md` reference | missing or unreadable instructions block publish |
| CONF-003 | Skill metadata | name and description are present and bounded |
| CONF-004 | Skill path containment | symlink/path traversal outside root is rejected |
| CONF-005 | Plugin manifest | version, dependencies and permissions validate |
| CONF-006 | Schedule schema | timezone, retry and permission mode validate |
| CONF-007 | MCP config | credentials are references, never plaintext defaults |
| CONF-008 | version compatibility | unsupported schema versions are rejected with a clear error |

### 6.3 Agent Runtime

| ID | Check | Required result |
|---|---|---|
| AGT-001 | supervisor routing | each reference intent reaches the expected Agent |
| AGT-002 | context loading | correct instruction and Skill versions are recorded |
| AGT-003 | run state machine | every transition is durable and valid |
| AGT-004 | cancellation | cancel stops new tool calls and records final state |
| AGT-005 | timeout | over-limit run becomes `failed` or `cancelled`, not success |
| AGT-006 | subagent budget | depth, count, steps and runtime are enforced |
| AGT-007 | output schema | invalid subagent output is rejected or repaired safely |
| AGT-008 | restart recovery | worker restart does not duplicate non-idempotent writes |

### 6.4 MCP and tools

| ID | Check | Required result |
|---|---|---|
| MCP-001 | server health | unavailable server is visible and cannot silently pass |
| MCP-002 | tool schema | invalid arguments are rejected before external call |
| MCP-003 | tool allowlist | unapproved tool cannot execute |
| MCP-004 | tool risk class | write/external/destructive classification is enforced |
| MCP-005 | timeout/retry | retry is limited and safe for tool idempotency class |
| MCP-006 | credentials | model never receives raw credentials |
| MCP-007 | tenant scope | tool cannot read another workspace/tenant |
| MCP-008 | provenance | result includes source, observed time and external id where applicable |
| MCP-009 | arbitrary SQL | raw SQL or unapproved view is rejected |
| MCP-010 | SSRF/network | private, loopback and unapproved destinations are blocked |
| MCP-011 | streamable HTTP | remote transport uses HTTPS host allowlist, timeout, no redirects and bounded responses |

### 6.5 Policy, approval and sandbox

| ID | Check | Required result |
|---|---|---|
| POL-001 | role matrix | each role has an explicit allow/deny result |
| POL-002 | fail closed | missing identity, tenant or policy denies access |
| POL-003 | write approval | no external write before approval |
| POL-004 | approval snapshot | changed arguments require re-approval |
| POL-005 | approval expiry | expired approval cannot be replayed |
| POL-006 | idempotency | repeated approval/run cannot duplicate external action |
| POL-007 | sandbox filesystem | access outside workspace is blocked |
| POL-008 | sandbox network | unapproved network target is blocked |
| POL-009 | shell | shell is denied unless explicit profile grants it |
| POL-010 | destructive action | destructive tool is denied by default |
| POL-011 | approval persistence | approval is scoped, expiring, argument-bound and token material is not stored in plaintext |

### 6.6 Skills, Plugins and packages

| ID | Check | Required result |
|---|---|---|
| EXT-001 | Skill discovery | only valid, visible Skills are offered to the Agent |
| EXT-002 | Skill execution | Skill cannot expand its own permissions |
| EXT-003 | plugin install | manifest, hash, version and permission review required |
| EXT-004 | plugin rollback | previous known-good version can be restored |
| EXT-005 | dependency conflict | incompatible Plugin/Skill versions are rejected |
| EXT-006 | untrusted package | package from unknown source is quarantined or blocked |
| EXT-007 | self-learning | generated Skill is proposal-only in production mode |
| EXT-008 | export/import | package imported into a clean environment is equivalent |
| EXT-009 | protected acceptance | manual target workflow uses protected environment secrets and emits redacted evidence |

### 6.7 Scheduler and long-running work

| ID | Check | Required result |
|---|---|---|
| SCH-001 | one-shot run | executes once and records result |
| SCH-002 | interval/cron | timezone and next-run calculation are correct |
| SCH-003 | pause/resume | paused task produces no new run |
| SCH-004 | missed run | catch-up policy is deterministic |
| SCH-005 | concurrency | duplicate schedule trigger does not duplicate run |
| SCH-006 | retry/backoff | retry limit and final failure are visible |
| SCH-007 | notification | only configured findings/failures notify |
| SCH-008 | scheduled permissions | unattended task cannot silently gain write access |
| SCH-009 | run isolation | scheduled run cannot pollute another run's context |
| SCH-010 | distributed queue | enqueue, claim, ack and abandoned-job reclaim preserve job identity |

### 6.8 Memory and RAG

| ID | Check | Required result |
|---|---|---|
| MEM-001 | scope isolation | user/project/workspace memory boundaries hold |
| MEM-002 | provenance | every recalled memory has source and timestamp |
| MEM-003 | deletion | deleted memory is no longer retrievable |
| MEM-004 | retention | expiry and retention policy are enforced |
| MEM-005 | write proposal | untrusted model cannot silently write durable memory |
| RAG-001 | ingestion | document version and content hash are recorded |
| RAG-002 | authorization | retrieval never crosses workspace scope |
| RAG-003 | citation | response cites source document/chunk when required |
| RAG-004 | prompt injection | document instructions cannot override runtime policy |
| RAG-005 | stale data | answer exposes observed/indexed time when relevant |

### 6.9 Security and privacy

| ID | Check | Required result |
|---|---|---|
| SEC-001 | repository secret scan | no active secret in working tree or history |
| SEC-002 | PII redaction | configured PII classes are masked before provider egress/logging |
| SEC-003 | prompt injection | known malicious fixtures are blocked or safely ignored |
| SEC-004 | path traversal | upload and Skill paths remain within allowed root |
| SEC-005 | SSRF | loopback/private/metadata destinations are rejected |
| SEC-006 | auth bypass | unauthenticated and wrong-role requests fail |
| SEC-007 | tenant isolation | conflicting tenant fixtures cannot cross-read/write |
| SEC-008 | log privacy | logs do not contain credentials or raw sensitive payloads |
| SEC-009 | dependency scan | critical/high findings are fixed or formally waived |
| SEC-010 | action pinning | CI actions use approved immutable references |
| SEC-011 | container hardening | image runs as non-root where possible and has no embedded secret |
| SEC-012 | supply chain | package integrity and source lineage are recorded |
| SEC-013 | resource abuse | expensive or body-bearing endpoints fail closed with bounded rate and size controls |
| SEC-015 | tenant-scoped audit reads | dashboard and run-event reads cannot mix tenants sharing one workspace |
| MCP-012 | result scope verification | a remote tool result with a mismatched workspace, tenant or record identity is rejected |
| MCP-013 | remote MCP readiness | POST-only endpoint returning 405 is reachable but auth/transport failures stay unhealthy |
| MCP-014 | remote MCP response bounds | oversized provenance and status fields are rejected before audit or user response construction |
| OPS-004 | attachment dependency readiness | `/ready` fails when metadata, object storage or the configured malware scanner is unavailable |

### 6.10 API, UI and observability

| ID | Check | Required result |
|---|---|---|
| API-001 | OpenAPI contract | generated schema matches implementation |
| API-002 | error states | blocked, approval, partial and external-pending states are explicit |
| API-003 | idempotency | repeated client request is safe |
| API-004 | resource limits | upload and provider-analysis endpoints enforce bounded body size and rate limits |
| API-005 | deployed scope | two real bearer principals cannot read each other's runs or attachments |
| UI-001 | approval UX | user sees exact action, target, arguments and risk |
| UI-002 | trace UX | internal user can inspect Agent, Tool, source and policy result |
| UI-003 | customer UX | customer cannot see internal secrets or hidden prompts |
| OBS-001 | trace continuity | request → run → tool → audit share trace identifiers |
| OBS-002 | failure visibility | connector failure is not shown as success |
| OBS-003 | metrics | latency, errors, approvals and queue depth are measurable |
| OBS-004 | redaction | observability output is privacy-safe |
| OBS-005 | metrics endpoint | authenticated metrics expose bounded counters without user-controlled labels |

### 6.11 Image evidence and provider egress

| ID | Check | Required result |
|---|---|---|
| IMG-001 | content validation | actual bytes, media type, size and pixel limits are enforced |
| IMG-002 | tenant scope | metadata, content and deletion cannot cross user/workspace/tenant scope |
| IMG-003 | storage safety | generated storage keys remain within the configured root and responses use `nosniff` |
| IMG-004 | lifecycle | create/delete events are append-only and retention/deletion behavior is documented |
| IMG-005 | malware scanning | production uploads are scanned before user or provider access |
| IMG-006 | provider egress | Vision/OCR calls require an approved provider, timeout, consent and provenance record |
| IMG-007 | browser flow | authenticated upload, preview, failure and delete-confirmation states pass browser E2E |
| IMG-008 | object storage | production attachment storage uses an encrypted, bounded, allowlisted object-store adapter |

### 6.12 Text model provider

| ID | Check | Required result |
|---|---|---|
| MOD-001 | provider boundary | model calls use a replaceable, HTTPS allowlisted adapter |
| MOD-002 | consent | API and schedules default to no external model processing |
| MOD-003 | grounding | only server-verified evidence is sent to the model |
| MOD-004 | output labeling | model prose is explicitly unverified and cannot become external confirmation |
| MOD-005 | failure state | provider timeout/error is visible as failure or partial success |

## 7. Reference end-to-end scenarios

### E2E-001 Customer order status

Given a synthetic customer and order, when the customer asks for order status:

1. The user is authenticated and scoped.
2. Supervisor selects Customer Service Agent.
3. Policy permits `erp.get_order_status`.
4. MCP returns an external order reference and observed time.
5. Response includes the verified status and source time.
6. Audit contains the complete trace.

Failure conditions:

- The Agent claims a status without a tool result.
- Another workspace's order is returned.
- Connector timeout is rendered as success.

### E2E-002 Approved return request

1. User requests a return.
2. Agent creates an Action Preview only.
3. Policy returns `approval_required`.
4. No ERP write occurs before approval.
5. Approver reviews exact normalized arguments.
6. Approved run uses an idempotency key.
7. ERP confirmation is stored.
8. A changed argument or expired approval is rejected.

### E2E-003 Prompt injection in knowledge document

1. Ingest a document containing malicious instructions.
2. Search retrieves the document as untrusted content.
3. Agent uses the content as evidence only.
4. Agent does not change policy, reveal secrets or invoke undeclared tools.
5. Audit records the source and any guardrail decision.

### E2E-004 Scheduled inventory briefing

1. Schedule runs in Asia/Taipei timezone.
2. Run uses read-only permissions.
3. Inventory Agent uses only allowlisted tools.
4. Duplicate scheduler delivery produces one effective run.
5. Notification is sent only when findings or failures exist.
6. Run history and evidence are retained.

### E2E-005 Protected target acceptance

Given a provisioned target deployment and two short-lived OIDC principals with
different workspace/tenant scopes, the protected manual acceptance workflow
MUST:

1. Verify `/health` and `/ready` on the target API.
2. Verify that the API reports the workspace and tenant claims established by
   each bearer token, without sending identity headers.
3. Execute one reviewed ERP order read through the deployed API and verify the
   owning principal can retrieve its run while the other principal receives
   `404`.
4. Upload one synthetic image for each principal, verify owner read/content,
   verify cross-scope metadata/content return `404`, and delete both fixtures.
5. Run the configured MCP, model, Vision/OCR, S3, ClamAV and OTLP smoke checks
   with bounded timeouts and record only redacted status evidence.

The workflow run, target environment, image digest, provider versions and
timestamps MUST be bound to the exact commit in the evidence index. A local
or synthetic run cannot satisfy this check.

## 8. CI workflow requirements

### Pull request workflow

MUST run：

1. format check
2. lint
3. typecheck
4. unit tests
5. manifest validation
6. secret scan
7. dependency audit
8. build
9. documentation link check

### Main branch workflow

MUST additionally run：

1. integration tests
2. Docker Compose smoke test
3. MCP contract tests
4. policy matrix tests
5. E2E reference flows
6. CodeQL or equivalent code scanning
7. container scan
8. SBOM generation

### Release workflow

MUST additionally verify：

1. clean checkout from tag
2. install-from-artifact
3. checksum or digest
4. migration from previous supported version
5. rollback or recovery procedure
6. generated release notes
7. no prohibited files in the artifact
8. release tag exactly matches a stable `MAJOR.MINOR.PATCH` package version

## 9. Release gates

### Gate A: Developer Preview

Required:

- all `INST-*`, `CONF-*`, `AGT-*`, `MCP-*` essential checks are `PASS`.
- no `SEC-001`, `SEC-006`, `SEC-007` failure.
- demo provider is synthetic or explicitly documented.
- README installation path works from a clean checkout.

### Gate B: Community Release

Required:

- all Gate A requirements.
- all `EXT-*`, `SCH-*`, `MEM-*`, `RAG-*` v1 checks are `PASS`.
- `SEC-001` through `SEC-013` are `PASS` or time-bound `WAIVED`.
- License and third-party inventory are complete.
- CONTRIBUTING, SECURITY and CODE_OF_CONDUCT exist.
- release artifact installs without private infrastructure.

### Gate C: Production-Oriented Release

Required:

- all prior gates.
- no open critical/high security issue without explicit risk acceptance.
- backup, recovery and migration are verified.
- external integrations have E7 evidence or are clearly marked unsupported.
- threat model and incident response procedure are published.
- at least one maintainer besides the original author can reproduce the release.

## 10. Failure and waiver rules

- A single required `FAIL` blocks its release gate.
- `UNVERIFIED` cannot be reported as `PASS`.
- `MISSING` security, license, secret scan or install evidence blocks public release.
- A waiver MUST include owner, risk, compensating control, expiry date and issue link.
- Waivers MUST NOT cover leaked secrets, tenant isolation failures or authentication bypasses.
- A green lint or unit-test job never overrides a failed security or release gate.

## 11. Traceability matrix

| SDD area | Validation IDs |
|---|---|
| Instruction hierarchy and manifests | `CONF-*`, `EXT-001`, `EXT-002` |
| Agent Runtime | `AGT-*`, `API-*`, `OBS-*`, `E2E-005` |
| MCP Gateway | `MCP-*`, `SEC-005`, `SEC-012` |
| Image evidence and provider egress | `IMG-*`, `API-004`, `API-005`, `SEC-013`, `E2E-005` |
| Policy and approval | `POL-*`, `E2E-002` |
| Scheduler | `SCH-*`, `E2E-004` |
| Memory and RAG | `MEM-*`, `RAG-*`, `E2E-003` |
| Open-source packaging | `INST-*`, `SEC-001`, release workflow |
| Observability | `OBS-*`, `API-002` |

## 12. Initial evidence index

以下是初始模板；實際執行結果必須寫入版本化的
`docs/validation/evidence-index.csv`，並在提交後以完整 commit SHA 取代
`NO_COMMIT_YET`。本模板不得被解讀為通過結果：

```csv
id,status,scope,commit,command,artifact,notes
INST-001,NOT_RUN,,,,,
CONF-001,NOT_RUN,,,,,
AGT-001,NOT_RUN,,,,,
MCP-003,NOT_RUN,,,,,
POL-003,NOT_RUN,,,,,
SCH-001,NOT_RUN,,,,,
SEC-001,NOT_RUN,,,,,
E2E-001,NOT_RUN,,,,,
```

## 13. Maintainer sign-off

每次公開 release MUST 由 maintainer 確認：

- [ ] SDD version and implementation version are aligned.
- [ ] Validation evidence uses the tagged commit.
- [ ] All required gates are `PASS` or documented `WAIVED`.
- [ ] No secret, customer data or private infrastructure is included.
- [ ] License and third-party notices are included.
- [ ] Install, upgrade and rollback instructions were tested.
- [ ] Known limitations are visible in release notes.
