import importlib
from typing import Any, Literal, cast
from urllib.parse import urlsplit

from opentelemetry.sdk._logs.export import LogRecordExporter
from opentelemetry.sdk.metrics.export import MetricExporter
from opentelemetry.sdk.trace.export import SpanExporter

from pyobservability.config import TelemetryExportConfig
from pyobservability.exceptions import MissingExtraError

Signal = Literal["traces", "logs", "metrics"]
OtlpProtocol = Literal["grpc", "http"]

_EXPORTER_CLASSES: dict[tuple[Signal, OtlpProtocol], tuple[str, str]] = {
    ("traces", "grpc"): (
        "opentelemetry.exporter.otlp.proto.grpc.trace_exporter",
        "OTLPSpanExporter",
    ),
    ("traces", "http"): (
        "opentelemetry.exporter.otlp.proto.http.trace_exporter",
        "OTLPSpanExporter",
    ),
    ("logs", "grpc"): ("opentelemetry.exporter.otlp.proto.grpc._log_exporter", "OTLPLogExporter"),
    ("logs", "http"): ("opentelemetry.exporter.otlp.proto.http._log_exporter", "OTLPLogExporter"),
    ("metrics", "grpc"): (
        "opentelemetry.exporter.otlp.proto.grpc.metric_exporter",
        "OTLPMetricExporter",
    ),
    ("metrics", "http"): (
        "opentelemetry.exporter.otlp.proto.http.metric_exporter",
        "OTLPMetricExporter",
    ),
}


def otlp_span_exporter(config: TelemetryExportConfig) -> SpanExporter:
    return cast(SpanExporter, _otlp_exporter("traces", config))


def otlp_log_exporter(config: TelemetryExportConfig) -> LogRecordExporter:
    return cast(LogRecordExporter, _otlp_exporter("logs", config))


def otlp_metric_exporter(config: TelemetryExportConfig) -> MetricExporter:
    return cast(MetricExporter, _otlp_exporter("metrics", config))


def _otlp_exporter(signal: Signal, config: TelemetryExportConfig) -> Any:
    module_name, class_name = _EXPORTER_CLASSES[(signal, config.protocol)]
    try:
        exporter_class = getattr(importlib.import_module(module_name), class_name)
    except ImportError as error:
        raise MissingExtraError("otlp", f"OTLP/{config.protocol} {signal} export") from error
    return exporter_class(**_options(signal, config))


def _options(signal: Signal, config: TelemetryExportConfig) -> dict[str, Any]:
    options: dict[str, Any] = {
        "headers": {name: value.get_secret_value() for name, value in config.headers.items()},
        "timeout": config.timeout_s,
    }
    if config.protocol == "grpc":
        explicit = {} if config.insecure is None else {"insecure": config.insecure}
        return {**options, "endpoint": config.endpoint, **explicit}
    return {**options, "endpoint": signal_endpoint(config.endpoint, signal)}


def signal_endpoint(endpoint: str | None, signal: Signal) -> str | None:
    """OTLP/HTTP base endpoints get the per-signal path; endpoints with a path are used as-is."""
    if endpoint is None or urlsplit(endpoint).path not in ("", "/"):
        return endpoint
    return f"{endpoint.rstrip('/')}/v1/{signal}"
