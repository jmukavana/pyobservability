import threading
from collections.abc import Mapping, Sequence
from typing import Any

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider as SdkTracerProvider
from opentelemetry.trace import NoOpTracerProvider, ProxyTracerProvider, Tracer, TracerProvider
from opentelemetry.util.types import AnyValue

from pyobservability.config import ProviderMode, ResolvedProviderMode
from pyobservability.exceptions import ConfigurationError


class DelegatingTracerProvider(TracerProvider):
    """Installed globally once; OpenTelemetry forbids replacing the global provider later.

    `configure()` points it at a fresh SDK provider and `shutdown()` at a no-op one, so tracers
    handed out earlier always follow the current configuration.
    """

    def __init__(self) -> None:
        self._delegate: TracerProvider = NoOpTracerProvider()
        self.generation = 0

    @property
    def delegate(self) -> TracerProvider:
        return self._delegate

    def delegate_to(self, provider: TracerProvider) -> None:
        self._delegate = provider
        self.generation += 1

    def get_tracer(
        self,
        instrumenting_module_name: str,
        instrumenting_library_version: str | None = None,
        schema_url: str | None = None,
        attributes: Mapping[str, AnyValue] | None = None,
    ) -> Tracer:
        return _DelegatingTracer(
            self, (instrumenting_module_name, instrumenting_library_version, schema_url, attributes)
        )


class _DelegatingTracer(Tracer):
    def __init__(self, provider: DelegatingTracerProvider, tracer_arguments: Sequence[Any]) -> None:
        self._provider = provider
        self._tracer_arguments = tracer_arguments
        self._tracer: Tracer = NoOpTracerProvider().get_tracer("")
        self._generation = -1

    def start_span(self, *args: Any, **kwargs: Any) -> trace.Span:
        return self._current().start_span(*args, **kwargs)

    def start_as_current_span(self, *args: Any, **kwargs: Any) -> Any:
        return self._current().start_as_current_span(*args, **kwargs)

    def _current(self) -> Tracer:
        if self._generation != self._provider.generation:
            self._tracer = self._provider.delegate.get_tracer(*self._tracer_arguments)
            self._generation = self._provider.generation
        return self._tracer


def resolve_provider_mode(
    requested: ProviderMode, global_tracer_provider: TracerProvider
) -> ResolvedProviderMode:
    installed_by_others = not isinstance(
        global_tracer_provider, ProxyTracerProvider | DelegatingTracerProvider
    )
    if requested == "own" and installed_by_others:
        raise ConfigurationError(
            "a global OpenTelemetry TracerProvider is already installed; "
            "use provider_mode='attach' or 'external'"
        )
    if requested != "auto":
        return requested
    if not installed_by_others:
        return "own"
    if isinstance(global_tracer_provider, SdkTracerProvider):
        return "attach"
    return "external"


_install_lock = threading.Lock()


def global_delegating_provider() -> DelegatingTracerProvider:
    with _install_lock:
        current = trace.get_tracer_provider()
        if isinstance(current, DelegatingTracerProvider):
            return current
        if not isinstance(current, ProxyTracerProvider):
            raise ConfigurationError("a foreign global TracerProvider is already installed")
        delegating = DelegatingTracerProvider()
        trace.set_tracer_provider(delegating)
        return delegating
