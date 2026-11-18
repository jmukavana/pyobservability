"""Distributed tracing on the OpenTelemetry SDK."""

from pyobservability.tracing.decorators import traced
from pyobservability.tracing.factory import TracingComponent, get_tracer
from pyobservability.tracing.propagation import context_from_headers, inject_trace_headers
from pyobservability.tracing.provider import resolve_provider_mode

__all__ = [
    "TracingComponent",
    "context_from_headers",
    "get_tracer",
    "inject_trace_headers",
    "resolve_provider_mode",
    "traced",
]
