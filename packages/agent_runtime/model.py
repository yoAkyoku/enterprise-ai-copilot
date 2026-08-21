"""Replaceable, privacy-bounded text model provider contracts."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from .network import NoRedirectHandler, validated_https_endpoint


class ModelProviderError(RuntimeError):
    """Raised when a configured model cannot return a bounded completion."""


@dataclass(frozen=True)
class ModelCompletion:
    provider: str
    model: str
    text: str


class ModelProvider(Protocol):
    provider_id: str
    model: str
    requires_external_consent: bool

    def complete(
        self,
        query: str,
        evidence: Mapping[str, str],
        *,
        request_id: str,
        trace_id: str,
        run_id: str,
    ) -> ModelCompletion:
        """Generate a bounded explanation from user text and verified evidence."""

    def health(self) -> dict[str, str]:
        """Return configuration health without making an external call."""


class OpenAICompatibleModelProvider:
    """Call an allowlisted OpenAI-compatible chat-completions endpoint.

    The adapter is deliberately not an evidence verifier. The runtime sends
    only the user query and server-verified evidence, and labels the returned
    prose as unverified before exposing it to a caller.
    """

    provider_id = "openai-compatible"
    requires_external_consent = True

    def __init__(
        self,
        endpoint: str,
        api_key: str,
        model: str,
        *,
        allowed_hosts: Sequence[str],
        timeout_seconds: float = 30.0,
        max_output_chars: int = 4000,
    ) -> None:
        if (
            not api_key.strip()
            or len(api_key) > 4096
            or any(character in api_key for character in "\r\n")
        ):
            raise ValueError("model API key is invalid")
        if not model.strip() or len(model) > 200:
            raise ValueError("model name must be a non-empty bounded value")
        if timeout_seconds <= 0 or timeout_seconds > 120:
            raise ValueError("model timeout must be between 0 and 120 seconds")
        if max_output_chars < 128 or max_output_chars > 32_000:
            raise ValueError("model output limit is invalid")
        endpoint_path = endpoint.rstrip("/")
        if not endpoint_path.endswith("/chat/completions"):
            endpoint_path = f"{endpoint_path}/chat/completions"
        self.endpoint = validated_https_endpoint(
            endpoint_path,
            allowed_hosts,
            label="model",
            default_path="/v1/chat/completions",
        )
        self.api_key = api_key
        self.model = model.strip()
        self.timeout_seconds = timeout_seconds
        self.max_output_chars = max_output_chars

    def health(self) -> dict[str, str]:
        return {
            "provider": self.provider_id,
            "model": self.model,
            "status": "configured",
        }

    def complete(
        self,
        query: str,
        evidence: Mapping[str, str],
        *,
        request_id: str,
        trace_id: str,
        run_id: str,
    ) -> ModelCompletion:
        if not isinstance(query, str) or not query.strip() or len(query) > 4000:
            raise ModelProviderError("model query is invalid")
        if set(evidence) != {"order_id", "status", "source_id", "observed_at"} or any(
            not isinstance(value, str) or not value.strip() or len(value) > 512
            for value in evidence.values()
        ):
            raise ModelProviderError("model evidence is invalid")
        payload = {
            "model": self.model,
            "temperature": 0,
            "max_tokens": min(1024, self.max_output_chars),
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are an explanation assistant. Use only the verified evidence "
                        "provided below. Do not invent facts, IDs, dates, causes, or actions. "
                        "If the user asks for anything beyond the evidence, say it is unknown."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {"query": query, "verified_evidence": dict(evidence)},
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                },
            ],
        }
        request = urllib.request.Request(
            self.endpoint,
            data=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "X-Request-Id": request_id,
                "X-Trace-Id": trace_id,
                "X-Run-Id": run_id,
            },
            method="POST",
        )
        try:
            opener = urllib.request.build_opener(NoRedirectHandler)
            with opener.open(request, timeout=self.timeout_seconds) as response:
                raw = response.read(2 * 1024 * 1024 + 1)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ModelProviderError("model provider request failed") from exc
        if len(raw) > 2 * 1024 * 1024:
            raise ModelProviderError("model provider response exceeded the response limit")
        try:
            body = json.loads(raw)
            content = body["choices"][0]["message"]["content"]
            if isinstance(content, list):
                content = "".join(
                    part.get("text", "") for part in content if isinstance(part, dict)
                )
            if not isinstance(content, str) or not content.strip():
                raise ValueError("empty model content")
            text = content.strip()
        except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ModelProviderError("model provider returned an invalid response") from exc
        if len(text) > self.max_output_chars:
            raise ModelProviderError("model provider output exceeded the configured limit")
        return ModelCompletion(provider=self.provider_id, model=self.model, text=text)
