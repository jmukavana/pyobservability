import logging

from opentelemetry import metrics
from opentelemetry.metrics import Meter, MeterProvider, NoOpMeterProvider
from opentelemetry.sdk.metrics import MeterProvider as SdkMeterProvider
from opentelemetry.sdk.metrics.export import MetricReader, PeriodicExportingMetricReader
from opentelemetry.sdk.metrics.view import View

from pyobservability.config import ObservabilityConfig, ResolvedProviderMode
from pyobservability.core.resource import build_resource
from pyobservability.core.timeouts import to_millis
from pyobservability.exporters import metric_exporters
from pyobservability.metrics.provider import global_delegating_meter_provider, is_ours_or_unset
from pyobservability.security import Redactor
from pyobservability.security.policies import LabelPolicy, PermissivePolicy

ALL_INSTRUMENTS = "*"

_logger = logging.getLogger("pyobservability.metrics")


def get_meter(name: str, version: str = "") -> Meter:
    return metrics.get_meter(name, version)


class MetricsComponent:
    """Owns the meter provider unless another one was installed first.

    An SDK MeterProvider cannot gain readers after creation, so a foreign one is left untouched.
    """

    name = "metrics"

    def __init__(
        self,
        config: ObservabilityConfig,
        redactor: Redactor,
        provider_mode: ResolvedProviderMode,
        global_provider: MeterProvider,
    ) -> None:
        self._config = config
        self._redactor = redactor
        self._provider_mode = provider_mode
        self._global_provider = global_provider
        self._own_provider: SdkMeterProvider | None = None

    def start(self) -> None:
        if self._provider_mode == "external":
            return
        if not is_ours_or_unset(self._global_provider):
            _logger.info("a MeterProvider is already installed; metrics stay with it")
            return
        self._own_provider = self._new_sdk_provider()
        policy = LabelPolicy(self._config.metrics, self._config.security, self._redactor)
        global_delegating_meter_provider().delegate_to(self._own_provider, policy)

    def shutdown(self, timeout_s: float) -> None:
        if self._own_provider is None:
            return
        self._own_provider.shutdown(timeout_millis=to_millis(timeout_s))
        global_delegating_meter_provider().delegate_to(NoOpMeterProvider(), PermissivePolicy())

    def _new_sdk_provider(self) -> SdkMeterProvider:
        return SdkMeterProvider(
            metric_readers=self._readers(),
            resource=build_resource(self._config),
            views=[
                View(
                    instrument_name=ALL_INSTRUMENTS,
                    aggregation_cardinality_limit=self._config.metrics.max_cardinality_per_metric,
                )
            ],
            shutdown_on_exit=False,
        )

    def _readers(self) -> list[MetricReader]:
        settings = self._config.metrics
        if not settings.exports:
            return []
        exporter = metric_exporters.create(settings.exporter, config=settings)
        return [
            PeriodicExportingMetricReader(
                exporter,
                export_interval_millis=settings.export_interval_ms,
                export_timeout_millis=to_millis(settings.timeout_s),
            )
        ]
