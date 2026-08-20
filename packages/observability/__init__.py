"""Small dependency-free observability contracts for the API boundary."""

from .metrics import MetricsRegistry

__all__ = ["MetricsRegistry"]
