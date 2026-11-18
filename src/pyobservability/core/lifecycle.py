import time
from collections.abc import Sequence
from dataclasses import dataclass

from pyobservability.core.protocols import Component


@dataclass(frozen=True, slots=True)
class ComponentShutdown:
    name: str
    budget_s: float
    duration_s: float
    error: str | None = None

    @property
    def timed_out(self) -> bool:
        return self.duration_s > self.budget_s

    @property
    def succeeded(self) -> bool:
        return self.error is None and not self.timed_out


@dataclass(frozen=True, slots=True)
class ShutdownReport:
    components: tuple[ComponentShutdown, ...] = ()

    @property
    def succeeded(self) -> bool:
        return all(component.succeeded for component in self.components)


class LifecycleManager:
    """Starts components in order and stops them in reverse within a shared time budget."""

    def __init__(self, components: Sequence[Component] = (), *, shutdown_timeout_s: float) -> None:
        self._components = tuple(components)
        self._shutdown_timeout_s = shutdown_timeout_s
        self._started: list[Component] = []

    def start(self) -> None:
        for component in self._components:
            try:
                component.start()
            except Exception:
                self.shutdown()
                raise
            self._started.append(component)

    def shutdown(self, timeout_s: float | None = None) -> ShutdownReport:
        stopping = list(reversed(self._started))
        self._started.clear()
        total_s = self._shutdown_timeout_s if timeout_s is None else timeout_s
        deadline = time.monotonic() + total_s
        results = []
        for position, component in enumerate(stopping):
            remaining_components = len(stopping) - position
            budget_s = max(deadline - time.monotonic(), 0.0) / remaining_components
            results.append(_shutdown_within(component, budget_s))
        return ShutdownReport(tuple(results))


def _shutdown_within(component: Component, budget_s: float) -> ComponentShutdown:
    started_at = time.monotonic()
    error = None
    try:
        component.shutdown(budget_s)
    except Exception as exception:
        error = f"{type(exception).__name__}: {exception}"
    return ComponentShutdown(
        name=component.name,
        budget_s=budget_s,
        duration_s=time.monotonic() - started_at,
        error=error,
    )
