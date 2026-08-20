"""Bounded counters with Prometheus text exposition."""

from __future__ import annotations

import re
import threading
from collections.abc import Mapping

_SAFE_NAME = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:]*$")
_SAFE_LABEL = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


class MetricsRegistry:
    """A process-local metrics registry with bounded, caller-controlled labels.

    Production deployments can replace this object with an OpenTelemetry or
    Prometheus adapter without changing API handlers. Values are never accepted
    as metric names or label names from user/model input.
    """

    def __init__(self) -> None:
        self._counters: dict[tuple[str, tuple[tuple[str, str], ...]], int] = {}
        self._lock = threading.Lock()

    def increment(self, name: str, labels: Mapping[str, str] | None = None, value: int = 1) -> None:
        if not _SAFE_NAME.fullmatch(name) or value < 1:
            raise ValueError("metric name and value are invalid")
        normalized: list[tuple[str, str]] = []
        for key, label_value in (labels or {}).items():
            if not _SAFE_LABEL.fullmatch(key) or len(label_value) > 100:
                raise ValueError("metric label is invalid")
            normalized.append((key, label_value))
        metric_key = (name, tuple(sorted(normalized)))
        with self._lock:
            self._counters[metric_key] = self._counters.get(metric_key, 0) + value

    def snapshot(self) -> dict[tuple[str, tuple[tuple[str, str], ...]], int]:
        with self._lock:
            return dict(self._counters)

    def prometheus(self) -> str:
        lines: list[str] = []
        for (name, labels), value in sorted(self.snapshot().items()):
            rendered_labels = ",".join(
                f'{key}="{label_value.replace(chr(92), chr(92) + chr(92)).replace(chr(34), chr(92) + chr(34))}"'
                for key, label_value in labels
            )
            lines.append(
                f"{name}{{{rendered_labels}}} {value}" if rendered_labels else f"{name} {value}"
            )
        return "\n".join(lines) + ("\n" if lines else "")
