from opentelemetry.sdk._logs.export import ConsoleLogRecordExporter, LogRecordExporter
from opentelemetry.sdk.metrics.export import ConsoleMetricExporter, MetricExporter
from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SpanExporter

from pyobservability.config import TelemetryExportConfig


def console_span_exporter(config: TelemetryExportConfig) -> SpanExporter:
    return ConsoleSpanExporter()


def console_log_exporter(config: TelemetryExportConfig) -> LogRecordExporter:
    return ConsoleLogRecordExporter()


def console_metric_exporter(config: TelemetryExportConfig) -> MetricExporter:
    return ConsoleMetricExporter()
