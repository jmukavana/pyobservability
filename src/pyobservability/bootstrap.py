import asyncio
import atexit
import warnings
from functools import cache
from typing import Any

from opentelemetry import metrics, trace

from pyobservability.audit.component import AuditComponent
from pyobservability.config import ObservabilityConfig, ResolvedProviderMode, load_config
from pyobservability.context.component import ContextComponent
from pyobservability.core.lifecycle import LifecycleManager, ShutdownReport
from pyobservability.core.protocols import Component
from pyobservability.exceptions import (
    AlreadyConfiguredError,
    ConfigurationError,
    PyObservabilityWarning,
)
from pyobservability.instrumentation import UnhandledExceptionHooks
from pyobservability.logging import LoggingComponent
from pyobservability.metrics import MetricsComponent
from pyobservability.runtime import Runtime, active_runtime
from pyobservability.security import Redactor
from pyobservability.tracing import TracingComponent, resolve_provider_mode


def configure(
    config: ObservabilityConfig | None = None, /, *, reconfigure: bool = False, **settings: Any
) -> Runtime:
    resolved = _resolve_config(config, settings)
    with active_runtime.lock:
        existing = active_runtime.current
        if existing is not None:
            if existing.config == resolved and not reconfigure:
                return existing
            if not reconfigure:
                raise AlreadyConfiguredError(
                    "pyobservability is already configured with different settings; "
                    "pass reconfigure=True to replace them"
                )
            shutdown()
        runtime = _start_runtime(resolved)
        active_runtime.current = runtime
        _shutdown_at_exit()
        return runtime


def shutdown(timeout_s: float | None = None) -> ShutdownReport:
    with active_runtime.lock:
        runtime, active_runtime.current = active_runtime.current, None
        if runtime is None:
            return ShutdownReport()
        return runtime.lifecycle.shutdown(timeout_s)


async def ashutdown(timeout_s: float | None = None) -> ShutdownReport:
    return await asyncio.to_thread(shutdown, timeout_s)


def _resolve_config(
    config: ObservabilityConfig | None, settings: dict[str, Any]
) -> ObservabilityConfig:
    if config is None:
        return load_config(**settings)
    if settings:
        raise ConfigurationError("pass either an ObservabilityConfig or keyword settings, not both")
    return config


def _start_runtime(config: ObservabilityConfig) -> Runtime:
    provider_mode = resolve_provider_mode(config.provider_mode, trace.get_tracer_provider())
    _warn_if_audit_events_can_be_lost(config)
    redactor = Redactor(config.security)
    lifecycle = LifecycleManager(
        _components_for(config, redactor, provider_mode),
        shutdown_timeout_s=config.shutdown_timeout_s,
    )
    lifecycle.start()
    return Runtime(
        config=config, provider_mode=provider_mode, redactor=redactor, lifecycle=lifecycle
    )


def _components_for(
    config: ObservabilityConfig, redactor: Redactor, provider_mode: ResolvedProviderMode
) -> list[Component]:
    components: list[Component] = [ContextComponent(config.context)]
    if config.metrics.enabled:
        components.append(
            MetricsComponent(config, redactor, provider_mode, metrics.get_meter_provider())
        )
    if config.logging.enabled:
        components.append(LoggingComponent(config, redactor))
    if config.logging.capture_unhandled_exceptions:
        components.append(UnhandledExceptionHooks())
    if config.tracing.enabled:
        components.append(
            TracingComponent(config, redactor, provider_mode, trace.get_tracer_provider())
        )
    if config.auditing.enabled:
        components.append(AuditComponent(config, redactor))
    return components


def _warn_if_audit_events_can_be_lost(config: ObservabilityConfig) -> None:
    if config.auditing.enabled and not config.auditing.is_durable:
        warnings.warn(
            "audit events are held in memory and will be lost if the process crashes; "
            "set auditing.durability.boundary to 'disk' or 'exporter' for durable delivery",
            PyObservabilityWarning,
            stacklevel=4,
        )


@cache
def _shutdown_at_exit() -> None:
    atexit.register(shutdown)
