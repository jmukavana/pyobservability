from collections.abc import Iterable
from typing import Any

import pytest
from opentelemetry import metrics
from opentelemetry.metrics import CallbackOptions, Observation
from opentelemetry.sdk.metrics import MeterProvider as SdkMeterProvider
from opentelemetry.sdk.metrics.export import (
    HistogramDataPoint,
    Metric,
    MetricExporter,
    MetricExportResult,
    MetricsData,
    NumberDataPoint,
)
from opentelemetry.sdk.trace.export import SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import pyobservability
from pyobservability import SecurityPolicyError, get_meter, get_tracer
from pyobservability.config import load_config
from pyobservability.exporters import metric_exporters, span_exporters
from pyobservability.exporters.health import EXPORT_ERRORS_METRIC
from pyobservability.metrics import MetricsComponent
from pyobservability.metrics.provider import SECONDS_BUCKETS
from pyobservability.security import Redactor

MEMORY_EXPORTER = "memory-metrics"
FAILING_EXPORTER = "failing-spans"


class CollectingMetricExporter(MetricExporter):
    def __init__(self) -> None:
        super().__init__()
        self.exports: list[MetricsData] = []

    def export(
        self, metrics_data: MetricsData, timeout_millis: float = 10_000, **_: Any
    ) -> MetricExportResult:
        self.exports.append(metrics_data)
        return MetricExportResult.SUCCESS

    def force_flush(self, timeout_millis: float = 10_000) -> bool:
        return True

    def shutdown(self, timeout_millis: float = 30_000, **_: Any) -> None:
        pass

    def metric(self, name: str) -> Metric:
        for data in self.exports:
            for resource_metrics in data.resource_metrics:
                for scope_metrics in resource_metrics.scope_metrics:
                    for metric in scope_metrics.metrics:
                        if metric.name == name:
                            return metric
        raise AssertionError(f"metric {name!r} was not exported")

    def points(self, name: str) -> list[Any]:
        return list(self.metric(name).data.data_points)


class FailingSpanExporter(InMemorySpanExporter):
    def export(self, spans: Any) -> SpanExportResult:
        return SpanExportResult.FAILURE


class CurrentExporter:
    exporter = CollectingMetricExporter()


if MEMORY_EXPORTER not in metric_exporters.names():
    metric_exporters.register(MEMORY_EXPORTER, lambda **_: CurrentExporter.exporter)
    span_exporters.register(FAILING_EXPORTER, lambda **_: FailingSpanExporter())


@pytest.fixture
def exporter() -> CollectingMetricExporter:
    CurrentExporter.exporter = CollectingMetricExporter()
    return CurrentExporter.exporter


def configure_metrics(**metrics_settings: Any) -> None:
    pyobservability.configure(
        service_name="claims-service",
        auditing={"enabled": False},
        logging={"enabled": False},
        tracing={"enabled": False},
        metrics={"exporter": MEMORY_EXPORTER, **metrics_settings},
    )


def collected(exporter: CollectingMetricExporter) -> CollectingMetricExporter:
    pyobservability.shutdown()
    return exporter


class TestInstruments:
    def test_counter_values_and_resource_are_exported(
        self, exporter: CollectingMetricExporter
    ) -> None:
        configure_metrics()

        counter = get_meter("claims").create_counter("claims.approved", unit="{claim}")
        counter.add(2, {"channel": "web"})
        counter.add(3, {"channel": "web"})
        [point] = collected(exporter).points("claims.approved")
        resource = exporter.exports[0].resource_metrics[0].resource.attributes

        assert isinstance(point, NumberDataPoint)
        assert point.value == 5
        assert point.attributes == {"channel": "web"}
        assert resource["service.name"] == "claims-service"

    def test_up_down_counter_and_gauge(self, exporter: CollectingMetricExporter) -> None:
        configure_metrics()
        meter = get_meter("claims")

        queue = meter.create_up_down_counter("claims.queue.depth")
        queue.add(5)
        queue.add(-2)
        meter.create_gauge("claims.backlog.age").set(42)
        collected(exporter)

        assert exporter.points("claims.queue.depth")[0].value == 3
        assert exporter.points("claims.backlog.age")[0].value == 42

    def test_seconds_histograms_get_latency_buckets(
        self, exporter: CollectingMetricExporter
    ) -> None:
        configure_metrics()

        get_meter("claims").create_histogram("claims.duration", unit="s").record(0.3)
        [point] = collected(exporter).points("claims.duration")

        assert isinstance(point, HistogramDataPoint)
        assert tuple(point.explicit_bounds) == SECONDS_BUCKETS

    def test_explicit_bucket_advice_wins(self, exporter: CollectingMetricExporter) -> None:
        configure_metrics()

        histogram = get_meter("claims").create_histogram(
            "claims.amount", unit="s", explicit_bucket_boundaries_advisory=[1.0, 2.0]
        )
        histogram.record(1.5)

        assert tuple(collected(exporter).points("claims.amount")[0].explicit_bounds) == (1.0, 2.0)

    def test_observable_gauge_is_collected(self, exporter: CollectingMetricExporter) -> None:
        configure_metrics()

        def queue_size(_: CallbackOptions) -> Iterable[Observation]:
            return [Observation(7, {"queue": "claims"})]

        get_meter("claims").create_observable_gauge("claims.queue.size", callbacks=[queue_size])

        assert collected(exporter).points("claims.queue.size")[0].value == 7

    def test_generator_callbacks_are_rejected_clearly(self) -> None:
        configure_metrics()

        def generator_callback() -> Any:
            yield [Observation(1)]

        with pytest.raises(pyobservability.ConfigurationError, match="generators"):
            get_meter("claims").create_observable_counter(
                "claims.generated", callbacks=[generator_callback()]
            )


class TestLifecycle:
    def test_instruments_created_before_configure_record_afterwards(
        self, exporter: CollectingMetricExporter
    ) -> None:
        early_counter = get_meter("early").create_counter("early.events")
        configure_metrics()

        early_counter.add(1)

        assert collected(exporter).points("early.events")[0].value == 1

    def test_instruments_follow_reconfiguration(self, exporter: CollectingMetricExporter) -> None:
        counter = get_meter("claims").create_counter("claims.seen")
        configure_metrics()
        counter.add(1)
        pyobservability.shutdown()
        CurrentExporter.exporter = second_exporter = CollectingMetricExporter()
        configure_metrics()

        counter.add(10)

        assert collected(second_exporter).points("claims.seen")[0].value == 10

    def test_observables_are_re_registered_after_reconfiguration(
        self, exporter: CollectingMetricExporter
    ) -> None:
        get_meter("claims").create_observable_gauge(
            "claims.workers", callbacks=[lambda _: [Observation(3)]]
        )
        configure_metrics()
        pyobservability.shutdown()
        CurrentExporter.exporter = second_exporter = CollectingMetricExporter()
        configure_metrics()

        assert collected(second_exporter).points("claims.workers")[0].value == 3

    def test_disabled_metrics_record_nothing(self, exporter: CollectingMetricExporter) -> None:
        configure_metrics(enabled=False)

        get_meter("claims").create_counter("claims.ignored").add(1)
        collected(exporter)

        assert exporter.exports == []

    def test_foreign_meter_provider_is_left_alone(self, exporter: CollectingMetricExporter) -> None:
        config = load_config(
            service_name="svc", auditing={"enabled": False}, metrics={"exporter": MEMORY_EXPORTER}
        )
        component = MetricsComponent(config, Redactor(config.security), "own", SdkMeterProvider())

        component.start()
        component.shutdown(timeout_s=1)

        assert exporter.exports == []
        assert metrics.get_meter_provider() is not None


class TestLabelPolicy:
    @pytest.mark.parametrize("label", ["user_id", "request_id", "trace_id", "password", "email"])
    def test_forbidden_labels_are_stripped(
        self, exporter: CollectingMetricExporter, label: str
    ) -> None:
        configure_metrics()

        get_meter("claims").create_counter("claims.calls").add(1, {label: "x", "route": "/c"})

        assert collected(exporter).points("claims.calls")[0].attributes == {"route": "/c"}

    def test_forbidden_labels_can_be_rejected(self) -> None:
        configure_metrics(on_forbidden_label="reject")

        with pytest.raises(SecurityPolicyError, match="user_id"):
            get_meter("claims").create_counter("claims.calls").add(1, {"user_id": "u-1"})

    def test_only_allowed_labels_survive_when_allow_list_is_set(
        self, exporter: CollectingMetricExporter
    ) -> None:
        configure_metrics(allowed_label_keys=["route"])

        get_meter("claims").create_counter("claims.calls").add(1, {"route": "/c", "region": "eu"})

        assert collected(exporter).points("claims.calls")[0].attributes == {"route": "/c"}

    def test_custom_forbidden_labels(self, exporter: CollectingMetricExporter) -> None:
        configure_metrics(forbidden_label_keys=["customer_ref"])

        get_meter("claims").create_counter("claims.calls").add(1, {"customer_ref": "c-9"})

        assert collected(exporter).points("claims.calls")[0].attributes == {}

    def test_secret_label_values_are_redacted(self, exporter: CollectingMetricExporter) -> None:
        configure_metrics()

        get_meter("claims").create_counter("claims.calls").add(1, {"caller": "Bearer abc.def"})

        assert collected(exporter).points("claims.calls")[0].attributes == {"caller": "[REDACTED]"}

    def test_observation_labels_are_policed(self, exporter: CollectingMetricExporter) -> None:
        configure_metrics()

        get_meter("claims").create_observable_gauge(
            "claims.sessions", callbacks=[lambda _: [Observation(1, {"session_id": "s-1"})]]
        )

        assert collected(exporter).points("claims.sessions")[0].attributes == {}


class TestCardinality:
    def test_label_sets_beyond_the_limit_collapse_into_overflow(
        self, exporter: CollectingMetricExporter
    ) -> None:
        configure_metrics(max_cardinality_per_metric=3)

        counter = get_meter("claims").create_counter("claims.by_route")
        for route in range(10):
            counter.add(1, {"route": f"/r{route}"})
        points = collected(exporter).points("claims.by_route")
        overflow = [point for point in points if point.attributes.get("otel.metric.overflow")]

        assert len(points) == 3 + 1
        assert overflow
        assert sum(point.value for point in points) == 10


def test_failed_span_exports_are_counted(exporter: CollectingMetricExporter) -> None:
    pyobservability.configure(
        service_name="svc",
        auditing={"enabled": False},
        logging={"enabled": False},
        tracing={"exporter": FAILING_EXPORTER},
        metrics={"exporter": MEMORY_EXPORTER},
    )

    with get_tracer(__name__).start_as_current_span("lost"):
        pass
    [point] = collected(exporter).points(EXPORT_ERRORS_METRIC)

    assert point.value == 1
    assert point.attributes == {"signal": "traces"}


def test_repeated_get_meter_reuses_one_meter() -> None:
    assert get_meter("claims", "1.0") is get_meter("claims", "1.0")
    assert get_meter("claims", "1.0") is not get_meter("claims", "2.0")


def test_each_forbidden_label_is_reported_once(capsys: pytest.CaptureFixture[str]) -> None:
    pyobservability.configure(
        service_name="svc",
        auditing={"enabled": False},
        tracing={"enabled": False},
        logging={"output": "stderr"},
        metrics={"exporter": MEMORY_EXPORTER},
    )
    counter = get_meter("claims").create_counter("claims.calls")

    for _ in range(3):
        counter.add(1, {"user_id": "u-1"})

    assert capsys.readouterr().err.count("dropped forbidden label 'user_id'") == 1
