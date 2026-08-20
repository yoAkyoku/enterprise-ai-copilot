"""Bounded, replaceable trace export contracts.

The runtime emits privacy-safe span records through this boundary. Production
can use the OTLP/HTTP adapter while tests use the in-memory sink; exporter
failures never turn a completed business operation into a false connector
success, and are surfaced as audit evidence by the caller.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from packages.agent_runtime.network import NoRedirectHandler, validated_https_endpoint

_SAFE_ATTRIBUTE_KEY = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,127}$")
_SENSITIVE_ATTRIBUTE_KEY = re.compile(
    r"(?:token|secret|password|authorization|credential|api[_-]?key|prompt|image|content)",
    re.IGNORECASE,
)


class TraceExportError(RuntimeError):
    """Raised when a configured exporter cannot accept a span."""


@dataclass(frozen=True)
class TraceRecord:
    trace_id: str
    span_id: str
    name: str
    start_time_unix_nano: int
    end_time_unix_nano: int
    parent_span_id: str | None = None
    attributes: Mapping[str, str | int | bool] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.trace_id or len(self.trace_id) > 128:
            raise ValueError("trace_id is invalid")
        if not self.span_id or len(self.span_id) > 128:
            raise ValueError("span_id is invalid")
        if not self.name or len(self.name) > 128:
            raise ValueError("trace span name is invalid")
        if self.start_time_unix_nano <= 0 or self.end_time_unix_nano < self.start_time_unix_nano:
            raise ValueError("trace span timestamps are invalid")
        for key, value in self.attributes.items():
            if not isinstance(key, str) or not isinstance(value, (str, int, bool)):
                raise TypeError("trace attributes must be strings, integers or booleans")
            if (
                not _SAFE_ATTRIBUTE_KEY.fullmatch(key)
                or _SENSITIVE_ATTRIBUTE_KEY.search(key)
                or len(str(value)) > 256
            ):
                raise ValueError("trace attributes must be safe and bounded")


class TraceExporter(Protocol):
    def export(self, record: TraceRecord) -> None:
        """Export one bounded span record."""


class InMemoryTraceExporter:
    """Deterministic exporter for tests and local contract validation."""

    def __init__(self) -> None:
        self.records: list[TraceRecord] = []

    def export(self, record: TraceRecord) -> None:
        self.records.append(record)


class OtlpHttpTraceExporter:
    """Minimal OTLP/HTTP JSON exporter with strict egress controls."""

    def __init__(
        self,
        endpoint: str,
        *,
        allowed_hosts: Sequence[str],
        bearer_token: str | None = None,
        timeout_seconds: float = 5.0,
        max_bytes: int = 64 * 1024,
    ) -> None:
        if timeout_seconds <= 0 or timeout_seconds > 60:
            raise ValueError("trace exporter timeout is invalid")
        if max_bytes <= 0 or max_bytes > 1024 * 1024:
            raise ValueError("trace exporter payload bound is invalid")
        if bearer_token is not None and (not bearer_token.strip() or len(bearer_token) > 4096):
            raise ValueError("trace exporter bearer token is invalid")
        self.endpoint = validated_https_endpoint(
            endpoint,
            allowed_hosts,
            label="trace exporter",
            default_path="/v1/traces",
        )
        self.bearer_token = bearer_token
        self.timeout_seconds = timeout_seconds
        self.max_bytes = max_bytes
        self._opener = urllib.request.build_opener(NoRedirectHandler)

    @staticmethod
    def _otlp_id(value: str, length: int) -> str:
        normalized = value.lower()
        if len(normalized) == length and all(char in "0123456789abcdef" for char in normalized):
            return normalized
        return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]

    @staticmethod
    def _attribute(key: str, value: str | int | bool) -> dict[str, object]:
        if isinstance(value, bool):
            return {"key": key, "value": {"boolValue": value}}
        if isinstance(value, int):
            return {"key": key, "value": {"intValue": value}}
        return {"key": key, "value": {"stringValue": value}}

    def _payload(self, record: TraceRecord) -> bytes:
        payload = {
            "resourceSpans": [
                {
                    "resource": {
                        "attributes": [self._attribute("service.name", "enterprise-ai-copilot")]
                    },
                    "scopeSpans": [
                        {
                            "scope": {"name": "enterprise-ai-copilot"},
                            "spans": [
                                {
                                    "traceId": self._otlp_id(record.trace_id, 32),
                                    "spanId": self._otlp_id(record.span_id, 16),
                                    "parentSpanId": self._otlp_id(record.parent_span_id, 16)
                                    if record.parent_span_id
                                    else "",
                                    "name": record.name,
                                    "kind": 1,
                                    "startTimeUnixNano": record.start_time_unix_nano,
                                    "endTimeUnixNano": record.end_time_unix_nano,
                                    "attributes": [
                                        self._attribute(key, value)
                                        for key, value in sorted(record.attributes.items())
                                    ],
                                }
                            ],
                        }
                    ],
                }
            ]
        }
        encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        if len(encoded) > self.max_bytes:
            raise TraceExportError("trace payload exceeds configured bound")
        return encoded

    def export(self, record: TraceRecord) -> None:
        body = self._payload(record)
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.bearer_token:
            headers["Authorization"] = f"Bearer {self.bearer_token}"
        request = urllib.request.Request(self.endpoint, data=body, headers=headers, method="POST")
        try:
            with self._opener.open(request, timeout=self.timeout_seconds) as response:
                status_code = int(response.status)
                response.read(4096)
        except (OSError, urllib.error.URLError, ValueError) as exc:
            raise TraceExportError("trace exporter request failed") from exc
        if not 200 <= status_code < 300:
            raise TraceExportError(f"trace exporter returned HTTP {status_code}")


def new_span_id() -> str:
    """Create a short opaque span id without exposing user data."""

    return hashlib.sha256(f"{time.time_ns()}".encode("ascii")).hexdigest()[:16]
