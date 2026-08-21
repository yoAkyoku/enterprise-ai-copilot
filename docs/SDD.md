# Enterprise Agent Operating Platform

## Software Design Document

- Status: production-track draft 0.2
- Audience: maintainers, contributors, integrators, security reviewers
- Repository intent: open-source, self-hostable enterprise Agent platform
- Current implementation status: production-track package with a verified local
  runtime, Web console, durable run/audit/approval adapters, image evidence
  flow, OIDC/JWKS boundary, S3-compatible blob boundary and metrics endpoint;
  external production gates remain explicit in the release checklist
- Normative terms: `MUST` is required, `SHOULD` is recommended, and `MAY` is optional

## 1. Purpose

本專案提供一個可自架、可擴充、可稽核的企業 Agent 平台。使用者可以透過 Web、內部聊天或外部通道提出工作，平台再依照 Agent 設定、專案規則、Skills、MCP 工具、政策與審批流程完成任務。

平台的核心不是單一聊天機器人，而是一個可安裝與管理的 Agent Operating Layer：

```text
Channel / Schedule / Webhook
            |
      Identity Context
            |
 AGENTS.md + AGENT.md + Skills
            |
 Supervisor / Planner / Subagents
            |
  Policy + Sandbox + Approval
            |
        MCP Gateway
            |
 ERP / Database / Knowledge / Browser / Files
            |
 Verify + Artifact + Notification + Audit
```

## 2. Goals

### 2.1 Primary goals

1. 支援多個專責 Agent，而不是把所有能力塞進一個 prompt。
2. 以 `AGENTS.md`、`AGENT.md` 與 `SKILL.md` 將規則、人格與可重用流程分離。
3. 以 MCP Gateway 統一外部工具、權限、憑證、風險與稽核。
4. 支援一次性、週期性、事件型與 Webhook 觸發的長任務。
5. 對讀取、寫入、外傳及破壞性操作提供可配置審批。
6. 讓每次執行都能重建：誰提出、使用哪些規則、呼叫哪些工具、取得哪些來源、產生什麼結果。
7. 讓陌生開發者可透過 Docker、文件與測試完成安裝與第一次執行。
8. 提供可版本化的 Plugin 與 Agent package，支援社群擴充。

### 2.2 Non-goals for v1

- 不複製任何第三方產品的私有雲端服務、帳號系統或內部模型。
- 不預設允許無限制的主機 Shell、瀏覽器 Cookie、SSH key 或任意檔案存取。
- 不把模型產生的回答視為外部系統已成功更新的證據。
- 不在沒有明確資料隔離設計前宣稱支援正式多租戶。
- 不把自動產生的 Skill、Memory 或 Plugin 直接發布到正式環境。

## 3. Product scope

### 3.1 Initial personas

| Persona | Main use | Default permission |
|---|---|---|
| Customer | 查詢訂單、政策與案件狀態 | 僅限本人可見資料、讀取 |
| Sales | 客戶摘要、近期訂單、跟進草稿 | 讀取與草稿產生 |
| Manager | 庫存分析、低庫存與補貨建議 | 讀取與建議 |
| Admin | 管理 Agent、工具、政策、審批與稽核 | 平台管理，不代表可繞過業務政策 |
| Maintainer | 維護程式碼、Plugin 與發布 | Repository 維護權，不自動取得業務資料權 |

### 3.2 Initial reference flows

1. Customer asks `Where is my order?` → Customer Service Agent → ERP MCP → verified order status.
2. Sales asks for a customer summary → Sales Agent → ERP MCP + curated Database MCP → cited summary.
3. Manager asks for low stock → Inventory Agent → Database/ERP MCP → stock evidence and reorder suggestion.
4. User asks to create a return or purchase order → Action Preview → Approval → idempotent external write → external confirmation.

## 4. Architecture

### 4.1 Reference deployment

```text
                            +------------------+
                            |  Web / Channels  |
                            +--------+---------+
                                     |
                            +--------v---------+
                            | API / Auth       |
                            | Conversation API |
                            +--------+---------+
                                     |
                            +--------v---------+
                            | Agent Runtime    |
                            | Supervisor       |
                            | Planner          |
                            | Subagent Runner  |
                            +---+----+----+----+
                                |    |    |
                 +--------------+    |    +----------------+
                 |                   |                     |
        +--------v--------+  +-------v--------+  +---------v---------+
        | Policy Engine   |  | Approval       |  | Audit / Trace      |
        | RBAC / ABAC     |  | Service        |  | Event Store        |
        +--------+--------+  +-------+--------+  +---------+---------+
                 |                   |                     |
                 +-------------------+---------------------+
                                     |
                            +--------v---------+
                            | MCP Gateway      |
                            | Tool Registry    |
                            | Credential Proxy |
                            +--+----+----+-----+
                               |    |    |
                    +----------+    |    +----------+
                    |               |               |
              +-----v-----+  +------v------+  +-----v------+
              | ERP MCP   |  | Knowledge   |  | Database   |
              | ERPNext   |  | MCP         |  | MCP        |
              +-----------+  +-------------+  +------------+
```

### 4.2 Deployment strategy

v1 SHOULD be a modular monolith with separate worker processes:

- `web`: UI and API gateway.
- `api`: authentication, conversations, Agent Runtime and policy decisions.
- `worker`: scheduled runs, ingestion, retries and long-running jobs.
- `scheduler`: polls reviewed schedule definitions and idempotently publishes
  due slots to Redis; it never selects identity from a queue payload.
- `postgres`: durable state, audit events and optional pgvector.
- `redis`: queue, locks, rate limits and ephemeral streaming state.
- `object storage`: documents, artifacts and exported packages; attachment
  bytes use the S3-compatible adapter in production.

當任務需要跨數小時或數天的 durable workflow 時，才引入 Temporal 或相等的 workflow engine；在此之前使用 PostgreSQL state machine + worker 即可。

### 4.3 Reference technology stack

這是參考實作，不是對使用者的硬性限制。此 checkout 的 production
Compose profile 採 PostgreSQL shared store，並由 migration service 先套用
checked-in migrations。SQLite 仍可供 local 或明確選擇的單節點部署使用，
不可由多個 API replicas 共用同一個 SQLite volume。PostgreSQL 的 failover、
row-level policy、backup/restore 與水平擴展仍須在目標環境驗證。

- Frontend: static HTML/CSS/JavaScript Web console served by the API; the
  browser boundary is intentionally replaceable by a typed SPA without
  changing the API/runtime contracts.
- API and runtime: FastAPI + Python.
- Persistence: PostgreSQL.
- Retrieval: pgvector adapter with a replaceable VectorStore interface.
- Queue: Redis-backed worker.
- Deployment: Docker Compose for local use; container images for production.
- Observability: structured JSON logs, metrics and OpenTelemetry-compatible traces.

Audit responses MUST apply both workspace and tenant scope. Legacy event rows
that do not carry an explicit tenant claim are excluded from tenant-scoped API
reads until they are reconciled; a workspace match alone is not sufficient.

## 5. Repository and configuration model

```text
enterprise-ai-copilot/
├─ AGENTS.md
├─ agents/
│  └─ <agent-id>/
│     ├─ AGENT.md
│     ├─ agent.yaml
│     └─ evals/
├─ .agents/skills/
│  └─ <skill-id>/
│     ├─ SKILL.md
│     ├─ scripts/
│     ├─ references/
│     └─ assets/
├─ plugins/
│  └─ <plugin-id>/
│     ├─ .codex-plugin/plugin.json
│     ├─ skills/
│     ├─ mcp/
│     └─ ui/
├─ mcp/
│  ├─ servers.yaml
│  └─ policies.yaml
├─ schedules/
├─ memory/
├─ apps/web/
├─ services/api/
├─ services/worker/
├─ packages/contracts/
├─ packages/agent-runtime/
├─ packages/policy-engine/
├─ packages/mcp-gateway/
├─ packages/retrieval/
├─ infra/
├─ tests/
└─ docs/
```

The current Web console is a static package served by the API. It uses the same
authenticated API boundary as other channels; it does not receive direct
database, filesystem or provider credentials.

### 5.1 Instruction hierarchy

平台 MUST 支援以下規則來源：

1. Global policy：部署或組織層級，不由 repository 任意覆寫。
2. Root `AGENTS.md`：repository-wide instructions。
3. Nested `AGENTS.md`：module-specific instructions。
4. `AGENT.md`：單一 Agent 的任務、語氣與限制。
5. `SKILL.md`：可重用的程序性工作流。
6. Runtime policy：權限、沙盒與平台強制限制，優先於所有文字指令。

文字檔不能授予高於 runtime policy 的權限。

### 5.2 `agent.yaml` contract

```yaml
id: inventory-agent
version: 1.0.0
name: Inventory Agent
description: Analyze inventory and produce evidence-backed reorder suggestions.
instructions: ./AGENT.md

skills:
  allow:
    - low-stock-analysis
    - reorder-recommendation

mcp:
  allow:
    - erp.get_inventory
    - db.inventory_summary
    - knowledge.search

sandbox:
  filesystem: workspace
  network: allowlist

approval:
  read: auto
  write: required
  external_send: required
  destructive: deny

limits:
  max_steps: 12
  max_runtime_seconds: 300
  max_subagents: 2
```

必填欄位：`id`、`version`、`name`、`instructions`。所有 permission 欄位 MUST 使用 fail-closed default。

### 5.3 `AGENT.md` contract

`AGENT.md` SHOULD 包含：

- Mission
- Allowed data and tools
- Must / Must not
- Escalation conditions
- Output schema
- Evidence and citation requirements
- Failure behavior

`AGENT.md` 不得放置 secrets、長期 token、不可公開的 customer data 或可繞過平台政策的指令。

### 5.4 `SKILL.md` contract

Skill 是可重用程序，不是權限授權。Skill MUST：

- 有 `name` 與明確 `description`。
- 說明何時觸發以及何時不應觸發。
- 定義輸入、步驟、輸出與錯誤處理。
- 對 scripts 指定允許的路徑與參數。
- 不把任意 Shell 或外部連線視為預設能力。
- 能被 validator 檢查大小、路徑、依賴與 metadata。

Skill 的可見性不等於執行授權；最終授權一定由 Policy Engine 與 Sandbox 決定。

### 5.5 Plugin contract

```json
{
  "name": "erpnext-operations",
  "version": "1.0.0",
  "description": "ERPNext customer, order, and inventory workflows",
  "skills": "./skills/",
  "mcp": "./mcp/",
  "permissions": {
    "network": ["erp.example.internal"],
    "tools": ["erp.get_order_status", "erp.get_inventory"]
  }
}
```

Plugin registry MUST record publisher, version, integrity hash, requested permissions, dependency versions and review status. Install、update、rollback MUST be auditable。

### 5.6 Schedule contract

```yaml
id: daily-inventory-report
version: 1.0.0
agent: inventory-agent

schedule:
  type: cron
  expression: "0 8 * * 1-5"
  timezone: Asia/Taipei

run:
  mode: isolated
  max_runtime_seconds: 300
  max_concurrency: 1
  retry_limit: 2
  catch_up: false
  allow_external_processing: false

permissions:
  mode: read_only

notify:
  channel: web
  only_if: finding_or_failure
```

Schedule MUST support idempotency, timezone, retry/backoff, timeout, concurrency, pause/resume, expiration, notification and run history。

## 6. Agent Runtime

### 6.1 Run lifecycle

```text
created
  -> queued
  -> loading_context
  -> planning
  -> policy_checked
  -> waiting_for_approval (optional)
  -> executing
  -> waiting_external (optional)
  -> verifying
  -> succeeded | partial_success | failed | blocked | cancelled
```

每個 state transition MUST 寫入 durable `run_events`。Worker 重啟後 MUST 能依事件與 checkpoint 恢復或安全地標示為需要人工處理。

### 6.2 Supervisor and subagents

Supervisor 負責意圖分類、工作拆解、Agent 選擇與結果合併，不直接繞過 Policy Engine。

Subagent invocation MUST 包含：

- parent run id
- child run id
- scoped context
- allowed skills
- allowed tools
- time/step/token budget
- cancellation signal
- expected output schema

Subagent 的結果 MUST 經過 schema validation 與 evidence verification，不能直接把自由文字當成完成證據。

### 6.3 Tool call contract

```json
{
  "trace_id": "trace-123",
  "run_id": "run-456",
  "tenant_id": "from-auth-context",
  "user_id": "from-auth-context",
  "agent_id": "inventory-agent",
  "tool": "db.inventory_summary",
  "arguments": {},
  "idempotency_key": "idem-789"
}
```

`tenant_id`、`user_id`、permission scope 與 credential reference MUST 由受信任 runtime 注入，不能由使用者文字、檔案或模型決定。

## 7. MCP Gateway

### 7.1 Supported transports

- Local STDIO for development.
- Streamable HTTP for deployed services.
- OAuth or workload identity for remote services.
- Secret references instead of plaintext credentials.

### 7.2 Tool risk classes

| Risk | Meaning | Default |
|---|---|---|
| `read` | 查詢或搜尋 | auto if authorized |
| `write` | 建立或修改外部資料 | approval required |
| `external_send` | 發送 Email、訊息或公開內容 | approval required |
| `destructive` | 刪除、退款、批次變更 | deny until explicitly enabled |

MCP Gateway MUST implement server registry、tool allowlist、tool schema validation、timeout、retry policy、health check、credential isolation、rate limit、audit and provenance。

Successful tool results MUST echo the trusted workspace and tenant scope injected
by the runtime, carry the requested record identity and provenance, and be
rejected before a run is marked successful when any of those values disagree.
A remote MCP readiness probe MAY receive HTTP 405 from a POST-only tool
endpoint; that means the endpoint is reachable. Authentication, redirect,
transport and other HTTP failures remain unhealthy.

Remote MCP、OIDC/JWKS、Vision/OCR and object-storage endpoints MUST use HTTPS,
an exact host allowlist, literal private/loopback/metadata host rejection,
no-redirect transport and bounded response reads. Custom ports are preserved
only after the same endpoint validation.

Production readiness MUST probe metadata storage, object storage and the
configured malware scanner; configuration presence alone is not sufficient for
`/ready`.

Text model calls MUST use a reviewed provider adapter, send only server-verified
evidence, require explicit per-request or per-schedule external-processing
consent, and label free-form model output as unverified. Model failure MUST NOT
be reported as external-system success.

Database MCP 只能使用 allowlisted read-only views 或 parameterized procedures，不得提供任意 SQL。

## 8. Policy, approval and sandbox

### 8.1 Authorization evaluation

Policy decision MUST evaluate：

```text
identity
+ tenant / workspace
+ role and permissions
+ agent identity
+ tool identity
+ data classification
+ operation risk
+ schedule context
+ sandbox mode
+ approval state
```

Policy result：`allow`、`deny`、`approval_required`、`step_up_auth_required`。

### 8.2 Approval record

Approval MUST snapshot：

- requested action
- normalized arguments
- affected resource
- data preview
- policy decision
- requester
- approver
- expiration
- idempotency key
- external confirmation

核准後的實際參數若與 snapshot 不一致，必須重新審批。

### 8.3 Sandbox

預設限制：

- filesystem: workspace only
- network: deny or explicit allowlist
- shell: deny unless tool profile grants it
- secrets: never expose raw values to model context
- browser: isolated profile, no personal cookies
- process: CPU、memory、time、output size quotas

## 9. Memory and knowledge

### 9.1 Memory scopes

- Session memory
- Run memory
- Project memory
- User memory
- Organization memory

每筆 memory MUST 有 `source`、`created_at`、`created_by`、`confidence`、`expires_at`、`visibility` 與 `deletion_policy`。

模型只能提出 memory mutation；正式寫入須經 scanner、policy，必要時經人工核准。

### 9.2 RAG provenance

每個引用來源 MUST 保存：

- document id and version
- chunk id
- source URI or storage key
- content hash
- indexed_at
- retrieval score
- tenant/workspace scope

向量相似度不能取代來源權威性、權限或最新狀態驗證。

## 10. Data model

核心 tables/entities：

| Entity | Purpose |
|---|---|
| `workspaces` | Agent、Skill、Plugin 與資料的隔離邊界 |
| `users` / `roles` / `permissions` | 身份與授權 |
| `agents` | Agent manifest、版本與狀態 |
| `skills` | Skill metadata、版本、來源與 integrity |
| `plugins` | Plugin manifest、依賴與安裝狀態 |
| `mcp_servers` / `tools` | Server、Tool、schema 與 policy |
| `conversations` / `messages` | 對話狀態 |
| `agent_runs` / `run_events` | durable 執行狀態 |
| `tool_calls` | 工具請求、結果、錯誤與 provenance |
| `approval_requests` | 寫入與高風險動作核准 |
| `schedules` / `schedule_runs` | 排程定義與歷史 |
| `memories` | 分層長期記憶 |
| `documents` / `chunks` / `source_refs` | 知識庫與引用 |
| `audit_events` | 不可任意修改的操作紀錄 |

所有 business data MUST 有 workspace/tenant scope；single-tenant demo 也 MUST 保留此欄位與 query boundary。

## 11. API surface

建議使用 `/api/v1`，並透過 OpenAPI 發布 contract：

```text
POST   /api/v1/runs
GET    /api/v1/runs/{id}
POST   /api/v1/runs/{id}/cancel
GET    /api/v1/runs/{id}/events

GET    /api/v1/agents
POST   /api/v1/agents/validate
POST   /api/v1/agents/{id}/publish

GET    /api/v1/skills
GET    /api/v1/plugins
POST   /api/v1/plugins/install

GET    /api/v1/mcp/servers
POST   /api/v1/mcp/servers/{id}/health

GET    /api/v1/schedules
POST   /api/v1/schedules/{id}/run
POST   /api/v1/schedules/{id}/pause

GET    /api/v1/approvals
POST   /api/v1/approvals
POST   /api/v1/approvals/{id}/approve
POST   /api/v1/approvals/{id}/reject

GET    /api/v1/audit/events

GET    /metrics
GET    /ready
```

API MUST return explicit states for `blocked`, `approval_required`, `partial_success` and `external_confirmation_pending`；不能以 HTTP 200 + generic success 表示未完成的外部操作。

## 12. Observability

每個 request/run/tool call MUST carry：

- `request_id`
- `trace_id`
- `run_id`
- `workspace_id`
- `agent_id`
- `skill_version`
- `plugin_version`
- `model_provider`
- `policy_decision`

禁止把 secrets、完整 token、未遮罩 PII、原始 credential 或完整 private prompt 寫入一般 logs。

核心 metrics：

- run success / failure / blocked rate
- approval wait time
- tool latency and error rate
- external confirmation rate
- grounded answer rate
- policy deny rate
- prompt injection detection rate
- schedule missed-run rate
- model and tool cost
- queue depth and worker recovery count

## 13. Open-source packaging

Repository MUST include：

- `LICENSE`
- `README.md`
- `.env.example`
- Docker Compose
- seed data
- migration scripts
- test fixtures
- `CONTRIBUTING.md`
- `SECURITY.md`
- CI workflows
- changelog and release notes
- third-party license inventory

公開版本不得依賴私有 ERP、私有模型、內部網域或本機秘密才能通過基本測試。

## 14. Phased delivery

### v0.1 Developer Preview

- Agent Runtime
- instruction hierarchy
- Agent/Skill/MCP/Plugin/schedule contract validation
- fake ERP MCP
- FastAPI user-visible run API
- Docker packaging and CI workflow
- SQLite and PostgreSQL audit adapters
- basic policy, idempotency and audit

### v0.2 Safe Agent Platform

- Scheduler
- Memory
- Approval
- Sandbox
- Subagents
- Plugin manifest
- security and eval tests

### v0.3 Community Release

- CLI
- Agent/Plugin import/export
- public API contracts
- CI and release artifacts
- contributor workflow
- documentation site or complete docs

### v1.0

- stable compatibility policy
- durable long-running workflows
- production connectors
- PostgreSQL migration and backup strategy
- threat model and incident response
- documented extension SDK

## 15. Open decisions

The v0.1 preview has made the following decisions; the remaining items are
explicitly deferred：

1. License: Apache-2.0.
2. The first supported text model contract is OpenAI-compatible chat
   completions; operators must still review the concrete provider, egress
   policy, retention and data-processing terms before enabling it.
3. Whether v1 supports one or multiple workspaces.
4. Whether pgvector is enough for v1 or an external vector store is required.
5. Whether browser and host Shell are v1 or post-v1 features.
6. Whether scheduled write actions can ever run unattended.
7. Whether Plugin registry is local-only or public.
8. Supported OS matrix and minimum runtime versions: Python 3.12+, Windows and Linux are intended; Docker validation is pending on this Windows host.

## 16. References

- OpenAI: [AGENTS.md](https://developers.openai.com/codex/agent-configuration/agents-md)
- OpenAI: [Build skills](https://developers.openai.com/codex/build-skills)
- OpenAI: [Build plugins](https://developers.openai.com/codex/build-plugins)
- OpenAI: [Scheduled tasks](https://developers.openai.com/codex/automations)
- OpenAI: [MCP](https://developers.openai.com/codex/extend/mcp)
- GitHub: [Security and analysis settings](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/enabling-features-for-your-repository/managing-security-and-analysis-settings-for-your-repository)
- GitHub: [Secure use of Actions](https://docs.github.com/en/actions/reference/security/secure-use)
