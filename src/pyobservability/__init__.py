"""Unified observability and auditing SDK for Python."""

from importlib.metadata import version

from pyobservability import audit, context
from pyobservability.bootstrap import ashutdown, configure, shutdown
from pyobservability.config import ObservabilityConfig
from pyobservability.core.lifecycle import ShutdownReport
from pyobservability.exceptions import (
    AlreadyConfiguredError,
    AuditDurabilityError,
    AuditValidationError,
    ConfigurationError,
    MissingExtraError,
    NotConfiguredError,
    PyObservabilityError,
    PyObservabilityWarning,
    SecurityPolicyError,
)
from pyobservability.instrumentation import instrument, uninstrument
from pyobservability.logging import get_logger
from pyobservability.metrics import get_meter
from pyobservability.runtime import Runtime
from pyobservability.tracing import get_tracer, traced

__version__ = version("pyobservability")

__all__ = [
    "AlreadyConfiguredError",
    "AuditDurabilityError",
    "AuditValidationError",
    "ConfigurationError",
    "MissingExtraError",
    "NotConfiguredError",
    "ObservabilityConfig",
    "PyObservabilityError",
    "PyObservabilityWarning",
    "Runtime",
    "SecurityPolicyError",
    "ShutdownReport",
    "__version__",
    "ashutdown",
    "audit",
    "configure",
    "context",
    "get_logger",
    "get_meter",
    "get_tracer",
    "instrument",
    "shutdown",
    "traced",
    "uninstrument",
]
