"""User-visible API for the Agent platform and its secure image workspace."""

from __future__ import annotations

import atexit
import hmac
import os
import re
import time
import urllib.parse
import uuid
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, File, Header, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from packages.agent_runtime import (
    AgentRuntime,
    ApprovalError,
    ApprovalNotFound,
    ApprovalService,
    ApprovalStatus,
    AuditEvent,
    AuditLog,
    AuthenticationError,
    IdentityContext,
    InMemoryRateLimiter,
    JwtHs256Authenticator,
    JwtJwksAuthenticator,
    RateLimiter,
    RunResult,
    RunStatus,
    RunStore,
    SQLiteApprovalStore,
    SqliteAuditStore,
    SQLiteRunStore,
    StoredRun,
    ToolRisk,
)
from packages.agent_runtime.network import is_disallowed_host
from packages.attachments import (
    AttachmentError,
    AttachmentNotFound,
    AttachmentRecord,
    AttachmentService,
    MalwareDetected,
    MalwareScanUnavailable,
    SQLiteAttachmentStore,
)
from packages.contracts import validate_repository
from packages.observability import MetricsRegistry, TraceExporter, TraceRecord, new_span_id
from packages.persistence import (
    PostgresApprovalStore,
    PostgresAttachmentStore,
    PostgresAuditStore,
    PostgresRunStore,
)
from packages.vision import (
    OpenAICompatibleVisionProvider,
    VisionAnalysisError,
    VisionConsentRequired,
    VisionNotConfigured,
    VisionService,
)

_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


class RunRequest(BaseModel):
    query: str = Field(min_length=1, max_length=4000)
    order_id: str | None = Field(default=None, min_length=1, max_length=128)
    allow_external_processing: bool = False


class RunResponse(BaseModel):
    status: str
    run_id: str
    trace_id: str
    agent_id: str
    message: str
    source_id: str | None = None
    observed_at: str | None = None
    external_ref: str | None = None


class AnalysisRequest(BaseModel):
    task: Literal["describe", "ocr"]
    allow_external_processing: bool = False
    prompt: str | None = Field(default=None, max_length=2000)


class ApprovalCreateRequest(BaseModel):
    tool_name: str = Field(min_length=1, max_length=256)
    arguments: dict[str, object] = Field(default_factory=dict)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=200)
    ttl_seconds: int = Field(default=900, ge=60, le=86400)


class ApprovalRejectRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=1000)


class RunService:
    def __init__(
        self, runtime: AgentRuntime, audit: AuditLog, store: RunStore | None = None
    ) -> None:
        self.runtime = runtime
        self.audit = audit
        self.store = store
        self.runs: dict[str, StoredRun] = {}
        self.idempotency: dict[tuple[str, str, str, str, str], str] = {}

    def execute(
        self,
        request: RunRequest,
        identity: IdentityContext,
        idempotency_key: str | None,
        request_id: str | None = None,
        trace_id: str | None = None,
    ) -> RunResult:
        if idempotency_key:
            key = (
                identity.workspace_id,
                identity.tenant_id,
                identity.user_id,
                identity.role,
                idempotency_key,
            )
            existing_run_id = self.idempotency.get(key)
            if existing_run_id:
                return self.runs[existing_run_id].result
            if self.store is not None:
                persisted = self.store.find_idempotent(identity, idempotency_key)
                if persisted is not None:
                    self.runs[persisted.result.run_id] = persisted
                    self.idempotency[key] = persisted.result.run_id
                    return persisted.result
        result = self.runtime.run(
            request.query,
            identity,
            order_id=request.order_id,
            request_id=request_id,
            trace_id=trace_id,
            allow_external_model_processing=request.allow_external_processing,
        )
        stored = StoredRun(result=result, identity=identity, idempotency_key=idempotency_key)
        if self.store is not None:
            persisted = self.store.save(stored)
            if persisted is not None:
                stored = persisted
                result = persisted.result
        self.runs[result.run_id] = stored
        if idempotency_key:
            self.idempotency[
                (
                    identity.workspace_id,
                    identity.tenant_id,
                    identity.user_id,
                    identity.role,
                    idempotency_key,
                )
            ] = result.run_id
        return result

    def get(self, run_id: str, identity: IdentityContext) -> StoredRun | None:
        stored = self.runs.get(run_id)
        if stored is None and self.store is not None:
            stored = self.store.get(run_id, identity)
            if stored is not None:
                self.runs[run_id] = stored
        if stored is None or (
            stored.identity.workspace_id,
            stored.identity.tenant_id,
            stored.identity.user_id,
        ) != (identity.workspace_id, identity.tenant_id, identity.user_id):
            return None
        return stored


def _header_identity(
    user_id: Annotated[str | None, Header(alias="X-User-Id")] = None,
    workspace_id: Annotated[str | None, Header(alias="X-Workspace-Id")] = None,
    tenant_id: Annotated[str | None, Header(alias="X-Tenant-Id")] = None,
    role: Annotated[str | None, Header(alias="X-Role")] = None,
) -> IdentityContext:
    values = (user_id, workspace_id, tenant_id, role)
    if any(value is None or not value.strip() for value in values):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="authenticated identity and workspace scope are required",
        )
    return IdentityContext(user_id, workspace_id, tenant_id, role)


def _payload(result: RunResult) -> dict[str, object]:
    return RunResponse(
        status=result.status.value,
        run_id=result.run_id,
        trace_id=result.trace_id,
        agent_id=result.agent_id,
        message=result.message,
        source_id=result.source_id,
        observed_at=result.observed_at,
        external_ref=result.external_ref,
    ).model_dump()


def _result_response(result: RunResult) -> JSONResponse:
    if result.status is RunStatus.SUCCEEDED:
        response_status = status.HTTP_200_OK
    elif result.status is RunStatus.BLOCKED:
        response_status = status.HTTP_403_FORBIDDEN
    elif result.status is RunStatus.CANCELLED:
        response_status = status.HTTP_409_CONFLICT
    else:
        response_status = status.HTTP_502_BAD_GATEWAY
    return JSONResponse(status_code=response_status, content=_payload(result))


def _attachment_payload(record: AttachmentRecord) -> dict[str, object]:
    """Expose attachment metadata without returning identity storage details."""

    return {
        "id": record.id,
        "filename": record.filename,
        "content_type": record.content_type,
        "image_format": record.image_format,
        "size_bytes": record.size_bytes,
        "sha256": record.sha256,
        "width": record.width,
        "height": record.height,
        "created_at": record.created_at,
        "content_url": f"/api/v1/attachments/{record.id}/content",
    }


def _attachment_service_required(service: AttachmentService | None) -> AttachmentService:
    if service is None:
        raise HTTPException(status_code=503, detail="attachment storage is not configured")
    return service


def _vision_service_required(service: VisionService | None) -> VisionService:
    if service is None:
        raise HTTPException(status_code=503, detail="Vision/OCR provider is not configured")
    return service


def _approval_service_required(service: ApprovalService | None) -> ApprovalService:
    if service is None:
        raise HTTPException(status_code=503, detail="approval service is not configured")
    return service


def _record_attachment_event(
    audit: AuditLog,
    event_type: str,
    identity: IdentityContext,
    attachment_id: str,
    payload: dict[str, object],
    request_id: str | None = None,
) -> None:
    trace_id = f"attachment-trace-{uuid.uuid4().hex}"
    audit.append(
        AuditEvent(
            event_type=event_type,
            request_id=request_id or f"attachment-request-{uuid.uuid4().hex}",
            trace_id=trace_id,
            run_id=f"attachment-{attachment_id}",
            workspace_id=identity.workspace_id,
            agent_id="web-console",
            payload={"attachment_id": attachment_id, "tenant_id": identity.tenant_id, **payload},
        )
    )


def _record_approval_event(
    audit: AuditLog,
    event_type: str,
    identity: IdentityContext,
    approval_id: str,
    payload: dict[str, object],
    request_id: str | None = None,
) -> None:
    audit.append(
        AuditEvent(
            event_type=event_type,
            request_id=request_id or f"approval-request-{uuid.uuid4().hex}",
            trace_id=f"approval-trace-{uuid.uuid4().hex}",
            run_id=f"approval-{approval_id}",
            workspace_id=identity.workspace_id,
            agent_id="approval-service",
            payload={"approval_id": approval_id, "tenant_id": identity.tenant_id, **payload},
        )
    )


def create_app(
    runtime: AgentRuntime,
    audit: AuditLog,
    *,
    auth_mode: str = "bearer",
    bearer_token: str | None = None,
    jwt_secret: str | None = None,
    jwt_issuer: str | None = None,
    jwt_audience: str | None = None,
    oidc_issuer: str | None = None,
    oidc_audience: str | None = None,
    oidc_jwks_uri: str | None = None,
    oidc_allowed_hosts: list[str] | tuple[str, ...] = (),
    platform_env: str | None = None,
    provider_mode: str | None = None,
    storage_mode: str | None = None,
    attachments: AttachmentService | None = None,
    vision: VisionService | None = None,
    run_store: RunStore | None = None,
    approval_service: ApprovalService | None = None,
    web_root: str | Path | None = None,
    upload_rate_limit: int = 30,
    analysis_rate_limit: int = 20,
    upload_limiter: RateLimiter | None = None,
    analysis_limiter: RateLimiter | None = None,
    rate_limit_mode: str = "in_memory",
    metrics: MetricsRegistry | None = None,
    trace_exporter: TraceExporter | None = None,
) -> FastAPI:
    if auth_mode not in {"bearer", "headers", "jwt_hs256", "oidc_jwks"}:
        raise ValueError("auth_mode must be bearer, jwt_hs256, oidc_jwks or headers")
    resolved_platform_env = (platform_env or os.getenv("AGENT_PLATFORM_ENV", "development")).lower()
    if resolved_platform_env in {"staging", "production"} and auth_mode not in {
        "jwt_hs256",
        "oidc_jwks",
    }:
        raise ValueError("staging and production require jwt_hs256 or oidc_jwks authentication")
    jwt_authenticator = (
        JwtHs256Authenticator(jwt_secret or "", issuer=jwt_issuer, audience=jwt_audience)
        if auth_mode == "jwt_hs256"
        else None
    )
    oidc_authenticator = (
        JwtJwksAuthenticator(
            oidc_issuer or "",
            audience=oidc_audience,
            jwks_uri=oidc_jwks_uri,
            allowed_hosts=oidc_allowed_hosts,
        )
        if auth_mode == "oidc_jwks"
        else None
    )

    def component_health(component: object | None) -> bool:
        healthcheck = getattr(component, "healthcheck", None)
        if healthcheck is None:
            return True
        try:
            return bool(healthcheck())
        except Exception:  # noqa: BLE001 - readiness must fail closed
            return False

    service = RunService(runtime, audit, run_store)
    upload_limiter = upload_limiter or InMemoryRateLimiter(upload_rate_limit)
    analysis_limiter = analysis_limiter or InMemoryRateLimiter(analysis_rate_limit)
    metrics_registry = metrics or MetricsRegistry()
    application = FastAPI(
        title="Enterprise Agent Operating Platform",
        version="0.2.0.dev0",
        description="Policy-checked Agent Runtime with tenant-scoped image attachments.",
    )

    @application.middleware("http")
    async def request_observability(request: Request, call_next):  # type: ignore[no-untyped-def]
        candidate = request.headers.get("X-Request-Id", "")
        request_id = (
            candidate if _SAFE_REQUEST_ID.fullmatch(candidate) else f"req-{uuid.uuid4().hex}"
        )
        request.state.request_id = request_id
        request.state.trace_id = f"trace-{uuid.uuid4().hex}"
        started = time.perf_counter()
        trace_started = time.time_ns()
        try:
            response = await call_next(request)
        except Exception:
            metrics_registry.increment(
                "http_requests_total",
                {"method": request.method, "status": "500"},
            )
            raise
        if trace_exporter is not None:
            try:
                route = request.scope.get("route")
                route_template = getattr(route, "path", None)
                if not isinstance(route_template, str) or not route_template.startswith("/"):
                    route_template = "unmatched"
                trace_exporter.export(
                    TraceRecord(
                        trace_id=request.state.trace_id,
                        span_id=new_span_id(),
                        name="http.request",
                        start_time_unix_nano=trace_started,
                        end_time_unix_nano=time.time_ns(),
                        attributes={
                            "http_method": request.method,
                            "http_route": route_template,
                            "http_status": response.status_code,
                            "request_id": request_id,
                        },
                    )
                )
            except Exception as exc:  # noqa: BLE001 - tracing cannot break API responses
                metrics_registry.increment("trace_export_failures_total")
                request.state.trace_export_error = type(exc).__name__
        metrics_registry.increment(
            "http_requests_total",
            {"method": request.method, "status": str(response.status_code)},
        )
        response.headers["X-Request-Id"] = request_id
        response.headers["X-Response-Time-Ms"] = str(int((time.perf_counter() - started) * 1000))
        return response

    @application.middleware("http")
    async def attachment_request_size_guard(request: Request, call_next):  # type: ignore[no-untyped-def]
        if (
            request.method == "POST"
            and request.url.path == "/api/v1/attachments"
            and attachments is not None
        ):
            content_length = request.headers.get("content-length")
            if content_length is not None:
                try:
                    request_bytes = int(content_length)
                except ValueError:
                    return JSONResponse(
                        status_code=400, content={"detail": "invalid Content-Length"}
                    )
                if request_bytes > attachments.max_bytes + 1024 * 1024:
                    return JSONResponse(
                        status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                        content={
                            "detail": "multipart image request exceeds the configured size limit"
                        },
                    )
        return await call_next(request)

    def request_identity(
        authorization: Annotated[str | None, Header()] = None,
        user_id: Annotated[str | None, Header(alias="X-User-Id")] = None,
        workspace_id: Annotated[str | None, Header(alias="X-Workspace-Id")] = None,
        tenant_id: Annotated[str | None, Header(alias="X-Tenant-Id")] = None,
        role: Annotated[str | None, Header(alias="X-Role")] = None,
    ) -> IdentityContext:
        if auth_mode == "headers":
            return _header_identity(user_id, workspace_id, tenant_id, role)
        if auth_mode == "jwt_hs256":
            try:
                return jwt_authenticator.authenticate(authorization)  # type: ignore[union-attr]
            except AuthenticationError as exc:
                raise HTTPException(
                    status_code=401, detail="valid bearer authentication is required"
                ) from exc
        if auth_mode == "oidc_jwks":
            try:
                return oidc_authenticator.authenticate(authorization)  # type: ignore[union-attr]
            except AuthenticationError as exc:
                raise HTTPException(
                    status_code=401, detail="valid OIDC bearer authentication is required"
                ) from exc
        if not bearer_token:
            raise HTTPException(
                status_code=503, detail="API bearer authentication is not configured"
            )
        expected = f"Bearer {bearer_token}"
        if authorization is None or not hmac.compare_digest(authorization, expected):
            raise HTTPException(status_code=401, detail="valid bearer authentication is required")
        return IdentityContext(
            user_id="demo-user",
            workspace_id="demo-workspace",
            tenant_id="demo-tenant",
            role="customer",
        )

    @application.get("/health")
    def health() -> dict[str, str]:
        model_health = runtime.model_health()
        return {
            "status": "ok",
            "provider_mode": provider_mode or os.getenv("AGENT_PROVIDER_MODE", "synthetic"),
            "storage_mode": storage_mode or os.getenv("AGENT_STORAGE_MODE", "memory"),
            "auth_mode": auth_mode,
            "attachments": "configured" if attachments is not None else "unavailable",
            "object_storage": attachments.storage_mode
            if attachments is not None
            else "unavailable",
            "malware_scanner": attachments.scanner.scanner_id
            if attachments is not None
            else "unavailable",
            "attachment_retention_days": str(attachments.retention_days)
            if attachments is not None
            else "0",
            "rate_limit_mode": rate_limit_mode,
            "approvals": "configured" if approval_service is not None else "unavailable",
            "trace_exporter": "configured" if trace_exporter is not None else "unavailable",
            "model_provider": model_health.get("provider", "unknown"),
            "model": model_health.get("model", "unknown"),
            "model_status": model_health.get("status", "unknown"),
        }

    @application.get("/metrics", response_class=PlainTextResponse)
    def metrics_endpoint(
        identity: IdentityContext = Depends(request_identity),  # noqa: B008
    ) -> PlainTextResponse:
        del identity
        return PlainTextResponse(
            metrics_registry.prometheus(), media_type="text/plain; version=0.0.4"
        )

    @application.get("/ready")
    def ready() -> JSONResponse:
        """Report whether configured production dependencies are usable."""

        checks = {
            "auth": auth_mode in {"jwt_hs256", "oidc_jwks"}
            if resolved_platform_env in {"staging", "production"}
            else True,
            "provider": runtime.gateway_health().get("status") == "healthy",
            "model_provider": runtime.model_health().get("status") == "configured",
            "attachments": attachments is not None,
            "object_storage": attachments is not None and attachments.storage_mode == "s3",
            "durable_runs": run_store is not None,
            "approvals": approval_service is not None,
            "persistent_storage": (
                (storage_mode or os.getenv("AGENT_STORAGE_MODE", "memory")) != "memory"
                and component_health(audit)
                and component_health(run_store)
                and component_health(getattr(approval_service, "store", None))
                and component_health(getattr(attachments, "store", None))
            ),
            "malware_scanning": attachments is not None and attachments.requires_scan,
            "attachment_retention": attachments is not None and attachments.retention_seconds > 0,
            "distributed_rate_limit": rate_limit_mode == "redis",
            "trace_exporter": trace_exporter is not None,
        }
        ready_status = (
            all(checks.values()) if resolved_platform_env in {"staging", "production"} else True
        )
        body = {"status": "ready" if ready_status else "not_ready", "checks": checks}
        return JSONResponse(
            status_code=status.HTTP_200_OK if ready_status else status.HTTP_503_SERVICE_UNAVAILABLE,
            content=body,
        )

    @application.get("/api/v1/dashboard")
    def dashboard(identity: IdentityContext = Depends(request_identity)) -> dict[str, object]:  # noqa: B008
        configured_attachments = _attachment_service_required(attachments)
        records = configured_attachments.list(identity)
        scoped_events = list(audit.list_events(workspace_id=identity.workspace_id))
        events = scoped_events[-12:]
        mcp_health = runtime.gateway_health()
        return {
            "workspace_id": identity.workspace_id,
            "tenant_id": identity.tenant_id,
            "agents": [{"id": runtime.agent_id, "status": "ready", "tool_count": 1}],
            "mcp": [
                {
                    "id": mcp_health.get("server_id", "mcp"),
                    "status": mcp_health.get("status", "unhealthy"),
                    "transport": mcp_health.get("transport", "unknown"),
                }
            ],
            "approvals": {
                "pending": approval_service.pending_count(identity)
                if approval_service is not None
                else 0,
                "mode": "explicit_for_writes",
            },
            "audit": {
                "event_count": len(scoped_events),
                "recent": [event.event_type for event in events],
            },
            "attachments": {
                "count": len(records),
                "items": [_attachment_payload(record) for record in records],
            },
        }

    @application.get("/api/v1/approvals")
    def list_approvals(
        approval_status: str | None = None,
        identity: IdentityContext = Depends(request_identity),  # noqa: B008
    ) -> dict[str, object]:
        configured_approvals = _approval_service_required(approval_service)
        if approval_status is not None and approval_status not in {
            ApprovalStatus.PENDING,
            ApprovalStatus.APPROVED,
            ApprovalStatus.REJECTED,
            ApprovalStatus.EXPIRED,
        }:
            raise HTTPException(status_code=422, detail="invalid approval status")
        records = configured_approvals.list(identity, status=approval_status)
        return {"items": [record.as_dict() for record in records], "count": len(records)}

    @application.post("/api/v1/approvals", status_code=status.HTTP_201_CREATED)
    def create_approval(
        request: ApprovalCreateRequest,
        http_request: Request,
        identity: IdentityContext = Depends(request_identity),  # noqa: B008
    ) -> dict[str, object]:
        configured_approvals = _approval_service_required(approval_service)
        definition = runtime.tool_definition(request.tool_name)
        if definition is None:
            raise HTTPException(status_code=404, detail="tool is not registered")
        if definition.risk is ToolRisk.READ:
            raise HTTPException(status_code=422, detail="read operations do not require approval")
        decision = runtime.policy_decision(identity, definition.name)
        if decision.outcome == "deny":
            raise HTTPException(
                status_code=403, detail="the requester is not authorized for this tool"
            )
        if decision.outcome != "approval_required":
            raise HTTPException(
                status_code=422, detail="the tool does not require an approval record"
            )
        try:
            record = configured_approvals.request(
                identity,
                tool_name=definition.name,
                arguments=request.arguments,
                risk=definition.risk,
                idempotency_key=request.idempotency_key,
                ttl_seconds=request.ttl_seconds,
            )
        except ApprovalError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        _record_approval_event(
            audit,
            "approval.requested",
            identity,
            record.id,
            {
                "tool": record.tool_name,
                "risk": record.risk.value,
                "arguments_hash": record.arguments_hash,
            },
            request_id=getattr(http_request.state, "request_id", None),
        )
        return record.as_dict()

    @application.post("/api/v1/approvals/{approval_id}/approve")
    def approve_approval(
        approval_id: str,
        http_request: Request,
        identity: IdentityContext = Depends(request_identity),  # noqa: B008
    ) -> JSONResponse:
        configured_approvals = _approval_service_required(approval_service)
        try:
            record, token = configured_approvals.approve(identity, approval_id)
        except ApprovalNotFound as exc:
            raise HTTPException(status_code=404, detail="approval was not found") from exc
        except ApprovalError as exc:
            code = 403 if "only manager or admin" in str(exc) else 409
            raise HTTPException(status_code=code, detail=str(exc)) from exc
        _record_approval_event(
            audit,
            "approval.approved",
            identity,
            record.id,
            {"tool": record.tool_name, "risk": record.risk.value},
            request_id=getattr(http_request.state, "request_id", None),
        )
        return JSONResponse(
            status_code=200,
            content={**record.as_dict(), "approval_token": token},
            headers={"Cache-Control": "private, no-store"},
        )

    @application.post("/api/v1/approvals/{approval_id}/reject")
    def reject_approval(
        approval_id: str,
        request: ApprovalRejectRequest,
        http_request: Request,
        identity: IdentityContext = Depends(request_identity),  # noqa: B008
    ) -> dict[str, object]:
        configured_approvals = _approval_service_required(approval_service)
        try:
            record = configured_approvals.reject(identity, approval_id)
        except ApprovalNotFound as exc:
            raise HTTPException(status_code=404, detail="approval was not found") from exc
        except ApprovalError as exc:
            code = 403 if "only manager or admin" in str(exc) else 409
            raise HTTPException(status_code=code, detail=str(exc)) from exc
        _record_approval_event(
            audit,
            "approval.rejected",
            identity,
            record.id,
            {"tool": record.tool_name, "risk": record.risk.value, "reason": request.reason},
            request_id=getattr(http_request.state, "request_id", None),
        )
        return record.as_dict()

    @application.post("/api/v1/runs")
    def create_run(
        request: RunRequest,
        http_request: Request,
        identity: IdentityContext = Depends(request_identity),  # noqa: B008
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> JSONResponse:
        if idempotency_key is not None and (
            not idempotency_key.strip() or len(idempotency_key) > 200
        ):
            raise HTTPException(status_code=422, detail="Idempotency-Key must be 1-200 characters")
        result = service.execute(
            request,
            identity,
            idempotency_key,
            request_id=getattr(http_request.state, "request_id", None),
            trace_id=getattr(http_request.state, "trace_id", None),
        )
        metrics_registry.increment("agent_runs_total", {"status": result.status.value})
        return _result_response(result)

    @application.get("/api/v1/runs/{run_id}")
    def get_run(
        run_id: str,
        identity: IdentityContext = Depends(request_identity),  # noqa: B008
    ) -> dict[str, object]:
        stored = service.get(run_id, identity)
        if stored is None:
            raise HTTPException(status_code=404, detail="run was not found")
        return _payload(stored.result)

    @application.get("/api/v1/runs/{run_id}/events")
    def get_events(
        run_id: str,
        identity: IdentityContext = Depends(request_identity),  # noqa: B008
    ) -> dict[str, object]:
        stored = service.get(run_id, identity)
        if stored is None:
            raise HTTPException(status_code=404, detail="run was not found")
        events = service.audit.list_events(run_id=run_id)
        return {
            "run_id": run_id,
            "trace_id": stored.result.trace_id,
            "events": [
                {
                    "event_type": event.event_type,
                    "request_id": event.request_id,
                    "trace_id": event.trace_id,
                    "run_id": event.run_id,
                    "workspace_id": event.workspace_id,
                    "agent_id": event.agent_id,
                    "payload": event.payload,
                    "created_at": event.created_at,
                }
                for event in events
            ],
        }

    @application.get("/api/v1/agents")
    def list_agents(identity: IdentityContext = Depends(request_identity)) -> dict[str, object]:  # noqa: B008
        del identity
        return {
            "agents": [
                {
                    "id": runtime.agent_id,
                    "version": "0.2.0",
                    "status": "developer_preview",
                    "skills": ["order-status"],
                    "tools": [runtime.tool_name],
                }
            ]
        }

    @application.post("/api/v1/agents/validate")
    def validate_agents(identity: IdentityContext = Depends(request_identity)) -> dict[str, object]:  # noqa: B008
        del identity
        root = Path(__file__).resolve().parents[2]
        reports = [report.as_dict() for report in validate_repository(root)]
        return {
            "valid": bool(reports) and all(report["valid"] for report in reports),
            "reports": reports,
        }

    @application.get("/api/v1/attachments")
    def list_attachments(
        identity: IdentityContext = Depends(request_identity),  # noqa: B008
    ) -> dict[str, object]:
        configured_attachments = _attachment_service_required(attachments)
        records = configured_attachments.list(identity)
        return {"items": [_attachment_payload(record) for record in records], "count": len(records)}

    @application.post("/api/v1/attachments", status_code=status.HTTP_201_CREATED)
    async def upload_attachment(
        http_request: Request,
        image: UploadFile = File(...),  # noqa: B008
        identity: IdentityContext = Depends(request_identity),  # noqa: B008
    ) -> dict[str, object]:
        configured_attachments = _attachment_service_required(attachments)
        allowed, retry_after = upload_limiter.check(
            f"{identity.workspace_id}:{identity.tenant_id}:{identity.user_id}"
        )
        if not allowed:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="image upload rate limit exceeded",
                headers={"Retry-After": str(retry_after)},
            )
        try:
            data = await image.read(configured_attachments.max_bytes + 1)
            record = configured_attachments.upload(
                identity,
                filename=image.filename or "upload",
                content_type=image.content_type or "",
                data=data,
            )
        except MalwareDetected as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
            ) from exc
        except MalwareScanUnavailable as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
            ) from exc
        except AttachmentError as exc:
            message = str(exc)
            code = (
                status.HTTP_413_CONTENT_TOO_LARGE
                if "limit" in message
                else status.HTTP_415_UNSUPPORTED_MEDIA_TYPE
            )
            raise HTTPException(status_code=code, detail=message) from exc
        _record_attachment_event(
            audit,
            "attachment.created",
            identity,
            record.id,
            {
                "sha256": record.sha256,
                "size_bytes": record.size_bytes,
                "content_type": record.content_type,
            },
            request_id=getattr(http_request.state, "request_id", None),
        )
        metrics_registry.increment("attachments_total", {"operation": "upload"})
        return _attachment_payload(record)

    @application.get("/api/v1/attachments/{attachment_id}")
    def get_attachment(
        attachment_id: str,
        identity: IdentityContext = Depends(request_identity),  # noqa: B008
    ) -> dict[str, object]:
        configured_attachments = _attachment_service_required(attachments)
        try:
            return _attachment_payload(configured_attachments.metadata(identity, attachment_id))
        except AttachmentNotFound as exc:
            raise HTTPException(status_code=404, detail="attachment was not found") from exc

    @application.get("/api/v1/attachments/{attachment_id}/content")
    def get_attachment_content(
        attachment_id: str,
        identity: IdentityContext = Depends(request_identity),  # noqa: B008
    ) -> Response:
        configured_attachments = _attachment_service_required(attachments)
        try:
            record, data = configured_attachments.read_content(identity, attachment_id)
        except AttachmentNotFound as exc:
            raise HTTPException(status_code=404, detail="attachment was not found") from exc
        except AttachmentError as exc:
            raise HTTPException(
                status_code=503, detail="attachment content is unavailable"
            ) from exc
        return Response(
            content=data,
            media_type=record.content_type,
            headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store"},
        )

    @application.delete(
        "/api/v1/attachments/{attachment_id}", status_code=status.HTTP_204_NO_CONTENT
    )
    def delete_attachment(
        attachment_id: str,
        http_request: Request,
        identity: IdentityContext = Depends(request_identity),  # noqa: B008
    ) -> Response:
        configured_attachments = _attachment_service_required(attachments)
        try:
            record = configured_attachments.delete(identity, attachment_id)
        except AttachmentNotFound as exc:
            raise HTTPException(status_code=404, detail="attachment was not found") from exc
        except AttachmentError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        _record_attachment_event(
            audit,
            "attachment.deleted",
            identity,
            record.id,
            {"sha256": record.sha256, "size_bytes": record.size_bytes},
            request_id=getattr(http_request.state, "request_id", None),
        )
        metrics_registry.increment("attachments_total", {"operation": "delete"})
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @application.post("/api/v1/attachments/{attachment_id}/analyze")
    def analyze_attachment(
        attachment_id: str,
        request: AnalysisRequest,
        http_request: Request,
        identity: IdentityContext = Depends(request_identity),  # noqa: B008
    ) -> dict[str, object]:
        configured_attachments = _attachment_service_required(attachments)
        configured_vision = _vision_service_required(vision)
        allowed, retry_after = analysis_limiter.check(
            f"{identity.workspace_id}:{identity.tenant_id}:{identity.user_id}"
        )
        if not allowed:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="image analysis rate limit exceeded",
                headers={"Retry-After": str(retry_after)},
            )
        try:
            record, data = configured_attachments.read_content(identity, attachment_id)
            result = configured_vision.analyze(
                record,
                data,
                task=request.task,
                prompt=request.prompt,
                allow_external_processing=request.allow_external_processing,
            )
        except AttachmentNotFound as exc:
            raise HTTPException(status_code=404, detail="attachment was not found") from exc
        except AttachmentError as exc:
            raise HTTPException(
                status_code=503, detail="attachment content is unavailable"
            ) from exc
        except VisionNotConfigured as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except VisionConsentRequired as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except VisionAnalysisError as exc:
            _record_attachment_event(
                audit,
                "attachment.analysis_failed",
                identity,
                attachment_id,
                {"task": request.task, "reason": str(exc)},
            )
            raise HTTPException(
                status_code=502, detail="Vision/OCR provider did not return a verified result"
            ) from exc
        _record_attachment_event(
            audit,
            "attachment.analyzed",
            identity,
            attachment_id,
            {
                "task": result.task,
                "provider": result.provider,
                "model": result.model,
                "observed_at": result.observed_at,
                "sha256": record.sha256,
            },
            request_id=getattr(http_request.state, "request_id", None),
        )
        metrics_registry.increment("attachments_total", {"operation": "analyze"})
        return {"attachment_id": attachment_id, **result.as_dict()}

    static_root = (
        Path(web_root)
        if web_root is not None
        else Path(__file__).resolve().parents[2] / "apps" / "web"
    )
    if static_root.is_dir():
        assets_root = static_root / "assets"
        if assets_root.is_dir():
            application.mount("/assets", StaticFiles(directory=assets_root), name="web-assets")

        @application.get("/", include_in_schema=False)
        def web_index() -> FileResponse:
            return FileResponse(static_root / "index.html")

        @application.get("/app", include_in_schema=False)
        def web_app() -> FileResponse:
            return FileResponse(static_root / "index.html")

    return application


def build_default_app() -> FastAPI:
    from services.bootstrap import build_runtime, build_trace_exporter

    root = Path(__file__).resolve().parents[2]
    platform_env = os.getenv("AGENT_PLATFORM_ENV", "development").lower()
    data_dir = Path(os.getenv("AGENT_DATA_DIR", str(root / ".data")))
    storage_mode = os.getenv("AGENT_STORAGE_MODE", "memory").lower()
    if storage_mode not in {"memory", "sqlite", "postgres"}:
        raise RuntimeError("AGENT_STORAGE_MODE must be memory, sqlite or postgres")
    database_url = os.getenv("AGENT_DATABASE_URL", "").strip()
    if storage_mode == "postgres" and not database_url:
        raise RuntimeError("AGENT_DATABASE_URL is required when AGENT_STORAGE_MODE=postgres")
    if platform_env in {"staging", "production"} and storage_mode not in {"sqlite", "postgres"}:
        raise RuntimeError("staging and production require AGENT_STORAGE_MODE=sqlite or postgres")
    audit_store = (
        PostgresAuditStore(database_url)
        if storage_mode == "postgres"
        else SqliteAuditStore(data_dir / "agent-platform.sqlite3")
    )
    audit = AuditLog(store=audit_store)
    trace_exporter = build_trace_exporter(platform_env)
    runtime, _ = build_runtime(audit=audit, trace_exporter=trace_exporter)
    attachment_root = Path(os.getenv("AGENT_ATTACHMENT_ROOT", str(data_dir / "attachments")))
    attachment_db = Path(os.getenv("AGENT_ATTACHMENT_DB", str(data_dir / "attachments.sqlite3")))
    max_bytes = int(os.getenv("AGENT_ATTACHMENT_MAX_BYTES", "10485760"))
    max_pixels = int(os.getenv("AGENT_ATTACHMENT_MAX_PIXELS", "25000000"))
    upload_rate_limit = int(os.getenv("AGENT_UPLOAD_RATE_LIMIT", "30"))
    analysis_rate_limit = int(os.getenv("AGENT_ANALYSIS_RATE_LIMIT", "20"))
    retention_default = "30" if platform_env in {"staging", "production"} else "0"
    retention_days = int(os.getenv("AGENT_ATTACHMENT_RETENTION_DAYS", retention_default))
    scanner_mode = os.getenv("AGENT_MALWARE_SCANNER", "disabled").lower()
    if scanner_mode not in {"disabled", "clamav"}:
        raise RuntimeError("AGENT_MALWARE_SCANNER must be disabled or clamav")
    from packages.attachments import ClamAvScanner, NoopAttachmentScanner

    scanner = (
        ClamAvScanner(
            host=os.getenv("AGENT_CLAMAV_HOST", "127.0.0.1"),
            port=int(os.getenv("AGENT_CLAMAV_PORT", "3310")),
            timeout_seconds=float(os.getenv("AGENT_CLAMAV_TIMEOUT_SECONDS", "10")),
        )
        if scanner_mode == "clamav"
        else NoopAttachmentScanner()
    )
    require_scan = os.getenv(
        "AGENT_ATTACHMENT_REQUIRE_SCAN",
        "true" if platform_env in {"staging", "production"} else "false",
    ).lower() in {"1", "true", "yes", "on"}
    if require_scan and scanner.scanner_id == "disabled":
        raise RuntimeError("production attachments require AGENT_MALWARE_SCANNER=clamav")
    if platform_env in {"staging", "production"} and retention_days <= 0:
        raise RuntimeError("production attachments require a positive retention period")
    attachment_storage = os.getenv("AGENT_ATTACHMENT_STORAGE", "filesystem").lower()
    if attachment_storage not in {"filesystem", "s3"}:
        raise RuntimeError("AGENT_ATTACHMENT_STORAGE must be filesystem or s3")
    blob_store = None
    if attachment_storage == "s3":
        bucket = os.getenv("AGENT_S3_BUCKET", "").strip()
        endpoint = os.getenv("AGENT_S3_ENDPOINT", "").strip()
        allowed_s3_hosts = {
            item.strip().lower()
            for item in os.getenv("AGENT_S3_ALLOWED_HOSTS", "").split(",")
            if item.strip()
        }
        if not bucket:
            raise RuntimeError("AGENT_S3_BUCKET is required when AGENT_ATTACHMENT_STORAGE=s3")
        if platform_env in {"staging", "production"} and not endpoint:
            raise RuntimeError("staging and production require an explicit AGENT_S3_ENDPOINT")
        if platform_env in {"staging", "production"} and not allowed_s3_hosts:
            raise RuntimeError("staging and production require AGENT_S3_ALLOWED_HOSTS")
        if endpoint:
            parsed_endpoint = urllib.parse.urlparse(endpoint)
            endpoint_host = (parsed_endpoint.hostname or "").lower()
            if (
                parsed_endpoint.scheme != "https"
                or not endpoint_host
                or endpoint_host not in allowed_s3_hosts
                or is_disallowed_host(endpoint_host)
                or parsed_endpoint.username
                or parsed_endpoint.password
                or parsed_endpoint.query
                or parsed_endpoint.fragment
            ):
                raise RuntimeError(
                    "AGENT_S3_ENDPOINT must be HTTPS and match AGENT_S3_ALLOWED_HOSTS"
                )
        if (
            platform_env in {"staging", "production"}
            and not os.getenv("AGENT_S3_KMS_KEY_ID", "").strip()
        ):
            raise RuntimeError("staging and production require AGENT_S3_KMS_KEY_ID")
        try:
            import boto3

            from packages.attachments import S3BlobStore

            client = boto3.client(
                "s3",
                endpoint_url=endpoint or None,
                region_name=os.getenv("AGENT_S3_REGION") or None,
            )
            blob_store = S3BlobStore(
                client,
                bucket,
                prefix=os.getenv("AGENT_S3_PREFIX", "attachments"),
                kms_key_id=os.getenv("AGENT_S3_KMS_KEY_ID") or None,
            )
        except ImportError as exc:
            raise RuntimeError("boto3 is required when AGENT_ATTACHMENT_STORAGE=s3") from exc
    if platform_env in {"staging", "production"} and attachment_storage != "s3":
        raise RuntimeError("production attachments require AGENT_ATTACHMENT_STORAGE=s3")
    rate_limit_mode = "in_memory"
    upload_limiter = None
    analysis_limiter = None
    redis_url = os.getenv("AGENT_REDIS_URL", "").strip()
    if platform_env in {"staging", "production"} and not redis_url:
        raise RuntimeError("production requires AGENT_REDIS_URL for distributed rate limiting")
    if redis_url:
        try:
            from redis import Redis

            from packages.agent_runtime import RedisRateLimiter

            redis_client = Redis.from_url(redis_url, socket_timeout=2, socket_connect_timeout=2)
        except ImportError as exc:
            raise RuntimeError(
                "redis package is required when AGENT_REDIS_URL is configured"
            ) from exc
        upload_limiter = RedisRateLimiter(redis_client, upload_rate_limit, prefix="agent:upload")
        analysis_limiter = RedisRateLimiter(
            redis_client, analysis_rate_limit, prefix="agent:analysis"
        )
        rate_limit_mode = "redis"
    attachment_service = AttachmentService(
        attachment_root,
        store=(
            PostgresAttachmentStore(database_url)
            if storage_mode == "postgres"
            else SQLiteAttachmentStore(attachment_db)
        ),
        blob_store=blob_store,
        scanner=scanner,
        max_bytes=max_bytes,
        max_pixels=max_pixels,
        retention_days=retention_days,
    )
    vision_endpoint = os.getenv("AGENT_VISION_ENDPOINT", "").strip()
    vision_key = os.getenv("AGENT_VISION_API_KEY", "").strip()
    vision_model = os.getenv("AGENT_VISION_MODEL", "").strip()
    vision_service: VisionService | None = None
    if any((vision_endpoint, vision_key, vision_model)):
        if not all((vision_endpoint, vision_key, vision_model)):
            raise RuntimeError("Vision endpoint, API key and model must be configured together")
        allowed_hosts = os.getenv("AGENT_VISION_ALLOWED_HOSTS", "").split(",")
        vision_service = VisionService(
            OpenAICompatibleVisionProvider(
                vision_endpoint,
                vision_key,
                vision_model,
                allowed_hosts=allowed_hosts,
                timeout_seconds=float(os.getenv("AGENT_VISION_TIMEOUT_SECONDS", "30")),
            )
        )
    atexit.register(attachment_service.close)
    run_store = (
        PostgresRunStore(database_url)
        if storage_mode == "postgres"
        else SQLiteRunStore(data_dir / "agent-platform.sqlite3")
    )
    atexit.register(run_store.close)
    approval_service = ApprovalService(
        PostgresApprovalStore(database_url)
        if storage_mode == "postgres"
        else SQLiteApprovalStore(data_dir / "agent-platform.sqlite3")
    )
    atexit.register(approval_service.close)
    atexit.register(audit_store.close)
    auth_mode = os.getenv("AGENT_AUTH_MODE", "bearer")
    oidc_allowed_hosts = [
        item.strip()
        for item in os.getenv("AGENT_OIDC_ALLOWED_HOSTS", "").split(",")
        if item.strip()
    ]
    if platform_env in {"staging", "production"} and auth_mode not in {"jwt_hs256", "oidc_jwks"}:
        raise RuntimeError("staging and production require jwt_hs256 or oidc_jwks authentication")
    if (
        platform_env in {"staging", "production"}
        and os.getenv("AGENT_PROVIDER_MODE", "synthetic").lower() == "synthetic"
    ):
        raise RuntimeError("staging and production require AGENT_PROVIDER_MODE=remote")
    return create_app(
        runtime,
        audit,
        auth_mode=auth_mode,
        bearer_token=os.getenv("AGENT_API_TOKEN"),
        jwt_secret=os.getenv("AGENT_JWT_SECRET"),
        jwt_issuer=os.getenv("AGENT_JWT_ISSUER"),
        jwt_audience=os.getenv("AGENT_JWT_AUDIENCE"),
        oidc_issuer=os.getenv("AGENT_OIDC_ISSUER"),
        oidc_audience=os.getenv("AGENT_OIDC_AUDIENCE"),
        oidc_jwks_uri=os.getenv("AGENT_OIDC_JWKS_URI") or None,
        oidc_allowed_hosts=oidc_allowed_hosts,
        platform_env=platform_env,
        provider_mode=os.getenv("AGENT_PROVIDER_MODE", "synthetic"),
        storage_mode=storage_mode,
        attachments=attachment_service,
        vision=vision_service,
        run_store=run_store,
        approval_service=approval_service,
        trace_exporter=trace_exporter,
        upload_rate_limit=upload_rate_limit,
        analysis_rate_limit=analysis_rate_limit,
        upload_limiter=upload_limiter,
        analysis_limiter=analysis_limiter,
        rate_limit_mode=rate_limit_mode,
    )


app = build_default_app()
