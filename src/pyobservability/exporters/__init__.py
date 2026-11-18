"""Lossy-tolerant telemetry exporters with bounded queues."""

from opentelemetry.sdk._logs.export import LogRecordExporter
from opentelemetry.sdk.metrics.export import MetricExporter
from opentelemetry.sdk.trace.export import SpanExporter

from pyobservability.config import CONSOLE_EXPORTER, OTLP_EXPORTER
from pyobservability.core.registry import Registry
from pyobservability.exporters.console import (
    console_log_exporter,
    console_metric_exporter,
    console_span_exporter,
)
from pyobservability.exporters.otlp import (
    otlp_log_exporter,
    otlp_metric_exporter,
    otlp_span_exporter,
)

span_exporters: Registry[SpanExporter] = Registry(
    "span exporter", entry_point_group="pyobservability.span_exporters"
)
log_exporters: Registry[LogRecordExporter] = Registry(
    "log exporter", entry_point_group="pyobservability.log_exporters"
)
metric_exporters: Registry[MetricExporter] = Registry(
    "metric exporter", entry_point_group="pyobservability.metric_exporters"
)

span_exporters.register(OTLP_EXPORTER, otlp_span_exporter)
span_exporters.register(CONSOLE_EXPORTER, console_span_exporter)
log_exporters.register(OTLP_EXPORTER, otlp_log_exporter)
log_exporters.register(CONSOLE_EXPORTER, console_log_exporter)
metric_exporters.register(OTLP_EXPORTER, otlp_metric_exporter)
metric_exporters.register(CONSOLE_EXPORTER, console_metric_exporter)

__all__ = ["log_exporters", "metric_exporters", "span_exporters"]
