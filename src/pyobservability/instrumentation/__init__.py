"""Auto-instrumentation wrappers for third-party clients."""

from pyobservability.instrumentation.exceptions import UnhandledExceptionHooks
from pyobservability.instrumentation.http import instrument, uninstrument

__all__ = ["UnhandledExceptionHooks", "instrument", "uninstrument"]
