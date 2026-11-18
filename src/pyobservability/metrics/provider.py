import threading
from collections.abc import Callable, Iterable, Sequence
from typing import Any

from opentelemetry import metrics
from opentelemetry.context import Context
from opentelemetry.metrics import (
    CallbackOptions,
    CallbackT,
    Counter,
    Histogram,
    Meter,
    MeterProvider,
    NoOpMeterProvider,
    ObservableCounter,
    ObservableGauge,
    ObservableUpDownCounter,
    Observation,
    UpDownCounter,
    _Gauge,
)
from opentelemetry.metrics._internal import _ProxyMeterProvider
from opentelemetry.util.types import Attributes

from pyobservability.exceptions import ConfigurationError
from pyobservability.security.policies import AttributePolicy, PermissivePolicy

SECONDS_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)
SECONDS = "s"


class DelegatingMeterProvider(MeterProvider):
    """Installed globally once, like the tracer provider; `configure()` swaps what it delegates to.

    Synchronous instruments rebind lazily on their next use. Observable instruments are
    re-registered eagerly, because only the SDK ever calls them; meters are therefore held
    strongly, since callers usually discard both the meter and the observable.
    """

    def __init__(self) -> None:
        self._delegate: MeterProvider = NoOpMeterProvider()
        self.policy: AttributePolicy = PermissivePolicy()
        self.generation = 0
        self._meters: dict[tuple[str, ...], _DelegatingMeter] = {}
        self._meters_lock = threading.Lock()

    @property
    def delegate(self) -> MeterProvider:
        return self._delegate

    def delegate_to(self, provider: MeterProvider, policy: AttributePolicy) -> None:
        self._delegate = provider
        self.policy = policy
        self.generation += 1
        with self._meters_lock:
            meters = list(self._meters.values())
        for meter in meters:
            meter.rebind_observables()

    def get_meter(
        self,
        name: str,
        version: str | None = None,
        schema_url: str | None = None,
        attributes: Attributes = None,
    ) -> Meter:
        key = (name, str(version), str(schema_url), repr(sorted((attributes or {}).items())))
        with self._meters_lock:
            if key not in self._meters:
                self._meters[key] = _DelegatingMeter(self, name, version, schema_url, attributes)
            return self._meters[key]


class _DelegatingMeter(Meter):
    def __init__(
        self,
        provider: DelegatingMeterProvider,
        name: str,
        version: str | None,
        schema_url: str | None,
        attributes: Attributes,
    ) -> None:
        super().__init__(name, version, schema_url)
        self.provider = provider
        self._arguments = (name, version, schema_url, attributes)
        self._meter: Meter = NoOpMeterProvider().get_meter(name)
        self._generation = -1
        self._observables: list[_DelegatingObservable] = []

    def current(self) -> Meter:
        if self._generation != self.provider.generation:
            self._meter = self.provider.delegate.get_meter(*self._arguments)
            self._generation = self.provider.generation
        return self._meter

    def rebind_observables(self) -> None:
        for observable in self._observables:
            observable.bind()

    def create_counter(self, name: str, unit: str = "", description: str = "") -> Counter:
        return _DelegatingCounter(self, name, unit, description)

    def create_up_down_counter(
        self, name: str, unit: str = "", description: str = ""
    ) -> UpDownCounter:
        return _DelegatingUpDownCounter(self, name, unit, description)

    def create_histogram(
        self,
        name: str,
        unit: str = "",
        description: str = "",
        *,
        explicit_bucket_boundaries_advisory: Sequence[float] | None = None,
    ) -> Histogram:
        if explicit_bucket_boundaries_advisory is None and unit == SECONDS:
            explicit_bucket_boundaries_advisory = SECONDS_BUCKETS
        return _DelegatingHistogram(
            self, name, unit, description, explicit_bucket_boundaries_advisory
        )

    def create_gauge(self, name: str, unit: str = "", description: str = "") -> _Gauge:
        return _DelegatingGauge(self, name, unit, description)

    def create_observable_counter(
        self,
        name: str,
        callbacks: Sequence[CallbackT] | None = None,
        unit: str = "",
        description: str = "",
    ) -> ObservableCounter:
        return self._observable(_DelegatingObservableCounter, name, callbacks, unit, description)

    def create_observable_up_down_counter(
        self,
        name: str,
        callbacks: Sequence[CallbackT] | None = None,
        unit: str = "",
        description: str = "",
    ) -> ObservableUpDownCounter:
        return self._observable(
            _DelegatingObservableUpDownCounter, name, callbacks, unit, description
        )

    def create_observable_gauge(
        self,
        name: str,
        callbacks: Sequence[CallbackT] | None = None,
        unit: str = "",
        description: str = "",
    ) -> ObservableGauge:
        return self._observable(_DelegatingObservableGauge, name, callbacks, unit, description)

    def _observable[O: _DelegatingObservable](
        self,
        kind: type[O],
        name: str,
        callbacks: Sequence[CallbackT] | None,
        unit: str,
        description: str,
    ) -> O:
        observable = kind(self, name, callbacks or (), unit, description)
        self._observables.append(observable)
        observable.bind()
        return observable


class _DelegatingSynchronous:
    _create_method: str

    def __init__(
        self, meter: _DelegatingMeter, name: str, unit: str, description: str, **advice: Any
    ) -> None:
        self._meter = meter
        self._name = name
        self._arguments = {"name": name, "unit": unit, "description": description, **advice}
        self._instrument: Any = None
        self._generation = -1

    def _forward(self, method: str, value: float, attributes: Attributes, context: Any) -> None:
        if self._generation != self._meter.provider.generation:
            self._instrument = getattr(self._meter.current(), self._create_method)(
                **self._arguments
            )
            self._generation = self._meter.provider.generation
        safe_attributes = self._meter.provider.policy.apply(self._name, attributes)
        getattr(self._instrument, method)(value, safe_attributes, context)


class _DelegatingCounter(_DelegatingSynchronous, Counter):
    _create_method = "create_counter"

    def add(
        self, amount: float, attributes: Attributes = None, context: Context | None = None
    ) -> None:
        self._forward("add", amount, attributes, context)


class _DelegatingUpDownCounter(_DelegatingSynchronous, UpDownCounter):
    _create_method = "create_up_down_counter"

    def add(
        self, amount: float, attributes: Attributes = None, context: Context | None = None
    ) -> None:
        self._forward("add", amount, attributes, context)


class _DelegatingHistogram(_DelegatingSynchronous, Histogram):
    _create_method = "create_histogram"

    def __init__(
        self,
        meter: _DelegatingMeter,
        name: str,
        unit: str,
        description: str,
        buckets: Sequence[float] | None,
    ) -> None:
        super().__init__(
            meter, name, unit, description, explicit_bucket_boundaries_advisory=buckets
        )

    def record(
        self, amount: float, attributes: Attributes = None, context: Context | None = None
    ) -> None:
        self._forward("record", amount, attributes, context)


class _DelegatingGauge(_DelegatingSynchronous, _Gauge):
    _create_method = "create_gauge"

    def set(
        self, amount: float, attributes: Attributes = None, context: Context | None = None
    ) -> None:
        self._forward("set", amount, attributes, context)


class _DelegatingObservable:
    _create_method: str

    def __init__(
        self,
        meter: _DelegatingMeter,
        name: str,
        callbacks: Sequence[CallbackT],
        unit: str,
        description: str,
    ) -> None:
        self._meter = meter
        self._name = name
        self._callbacks = [self._policed(callback) for callback in callbacks]
        self._unit = unit
        self._description = description

    def bind(self) -> None:
        getattr(self._meter.current(), self._create_method)(
            self._name, callbacks=self._callbacks, unit=self._unit, description=self._description
        )

    def _policed(self, callback: CallbackT) -> Callable[[CallbackOptions], Iterable[Observation]]:
        if not callable(callback):
            raise ConfigurationError(
                f"observable {self._name!r}: only callable callbacks are supported, not generators"
            )

        def observe(options: CallbackOptions) -> Iterable[Observation]:
            policy = self._meter.provider.policy
            return [
                Observation(observation.value, policy.apply(self._name, observation.attributes))
                for observation in callback(options)
            ]

        return observe


class _DelegatingObservableCounter(_DelegatingObservable, ObservableCounter):
    _create_method = "create_observable_counter"


class _DelegatingObservableUpDownCounter(_DelegatingObservable, ObservableUpDownCounter):
    _create_method = "create_observable_up_down_counter"


class _DelegatingObservableGauge(_DelegatingObservable, ObservableGauge):
    _create_method = "create_observable_gauge"


_install_lock = threading.Lock()


def is_ours_or_unset(provider: MeterProvider) -> bool:
    return isinstance(provider, DelegatingMeterProvider | _ProxyMeterProvider)


def global_delegating_meter_provider() -> DelegatingMeterProvider:
    with _install_lock:
        current = metrics.get_meter_provider()
        if isinstance(current, DelegatingMeterProvider):
            return current
        if not is_ours_or_unset(current):
            raise ConfigurationError("a foreign global MeterProvider is already installed")
        delegating = DelegatingMeterProvider()
        metrics.set_meter_provider(delegating)
        return delegating
