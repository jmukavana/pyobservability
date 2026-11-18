import importlib
from dataclasses import dataclass
from typing import Any

from pyobservability.exceptions import ConfigurationError, MissingExtraError


@dataclass(frozen=True, slots=True)
class _Instrumentation:
    module: str
    instrumentor: str
    extra: str

    def instrumentor_instance(self, target: str) -> Any:
        try:
            module = importlib.import_module(self.module)
        except ImportError as error:
            raise MissingExtraError(self.extra, f"{target} instrumentation") from error
        return getattr(module, self.instrumentor)()


_INSTRUMENTATIONS = {
    "requests": _Instrumentation(
        "opentelemetry.instrumentation.requests", "RequestsInstrumentor", "http"
    ),
    "httpx": _Instrumentation(
        "opentelemetry.instrumentation.httpx", "HTTPXClientInstrumentor", "http"
    ),
}


def instrument(*targets: str) -> None:
    """Enables OpenTelemetry auto-instrumentation; already-instrumented targets are skipped."""
    for instrumentor in _instrumentors(targets):
        if not instrumentor.is_instrumented_by_opentelemetry:
            instrumentor.instrument()


def uninstrument(*targets: str) -> None:
    for instrumentor in _instrumentors(targets):
        if instrumentor.is_instrumented_by_opentelemetry:
            instrumentor.uninstrument()


def _instrumentors(targets: tuple[str, ...]) -> list[Any]:
    unknown = sorted(set(targets) - _INSTRUMENTATIONS.keys())
    if unknown:
        known = ", ".join(sorted(_INSTRUMENTATIONS))
        raise ConfigurationError(f"unknown instrumentation targets {unknown}; supported: {known}")
    return [_INSTRUMENTATIONS[target].instrumentor_instance(target) for target in targets]
