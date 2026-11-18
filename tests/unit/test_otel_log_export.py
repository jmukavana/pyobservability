import logging
from typing import Any

import pytest
from opentelemetry._logs import SeverityNumber
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter

import pyobservability
from pyobservability import get_logger, get_tracer
from pyobservability.exporters import log_exporters, span_exporters
from pyobservability.exporters.console import console_span_exporter
from pyobservability.logging.handlers import severity_of

MEMORY_EXPORTER = "memory-logs"


def new_log_exporter() -> InMemoryLogRecordExporter:
    return InMemoryLogRecordExporter()  # type: ignore[no-untyped-call]  # OTel SDK is untyped here


class CurrentExporter:
    exporter = new_log_exporter()


if MEMORY_EXPORTER not in log_exporters.names():
    log_exporters.register(MEMORY_EXPORTER, lambda **_: CurrentExporter.exporter)
    span_exporters.register(MEMORY_EXPORTER, console_span_exporter)


@pytest.fixture
def exporter() -> InMemoryLogRecordExporter:
    CurrentExporter.exporter = new_log_exporter()
    return CurrentExporter.exporter


def configure_export(**logging_settings: Any) -> None:
    pyobservability.configure(
        service_name="claims-service",
        auditing={"enabled": False},
        logging={"export_to_otel": True, "output": "stderr", **logging_settings},
        tracing={"exporter": MEMORY_EXPORTER, "sampler": "always_on"},
    )


def exported_bodies(exporter: InMemoryLogRecordExporter) -> list[Any]:
    pyobservability.shutdown()
    return [data.log_record.body for data in exporter.get_finished_logs()]


def test_structlog_records_are_exported_as_redacted_dicts(
    exporter: InMemoryLogRecordExporter,
) -> None:
    configure_export()

    get_logger("claims").info("approved", claim_id="CLM-1", api_key="k-1")
    [body] = exported_bodies(exporter)

    assert body["message"] == "approved"
    assert body["claim_id"] == "CLM-1"
    assert body["api_key"] == "[REDACTED]"


def test_stdlib_records_are_exported_redacted(exporter: InMemoryLogRecordExporter) -> None:
    configure_export()

    try:
        raise ConnectionError("postgres://admin:hunter2@db refused")
    except ConnectionError:
        logging.getLogger("driver").exception("connect failed with Bearer abc.def")
    [body] = exported_bodies(exporter)

    assert body["message"] == "connect failed with [REDACTED]"
    assert body["exception"]["type"] == "ConnectionError"
    assert "hunter2" not in repr(body)


def test_exported_logs_are_correlated_with_the_active_span(
    exporter: InMemoryLogRecordExporter,
) -> None:
    configure_export()

    with get_tracer(__name__).start_as_current_span("work") as span:
        get_logger().info("inside span")
    pyobservability.shutdown()
    [record] = [data.log_record for data in exporter.get_finished_logs()]

    assert record.trace_id == span.get_span_context().trace_id


def test_private_structlog_attributes_are_not_exported(
    exporter: InMemoryLogRecordExporter,
) -> None:
    configure_export()

    get_logger().info("clean")
    pyobservability.shutdown()
    [record] = [data.log_record for data in exporter.get_finished_logs()]

    assert not any(key.startswith("_") for key in (record.attributes or {}))


def test_log_export_requires_an_active_tracing_exporter() -> None:
    with pytest.raises(pyobservability.ConfigurationError, match="export_to_otel"):
        pyobservability.configure(
            service_name="svc",
            auditing={"enabled": False},
            logging={"export_to_otel": True},
            tracing={"exporter": "none"},
        )


@pytest.mark.parametrize(
    ("level", "severity"),
    [
        (logging.DEBUG, SeverityNumber.DEBUG),
        (logging.INFO, SeverityNumber.INFO),
        (logging.WARNING, SeverityNumber.WARN),
        (logging.ERROR, SeverityNumber.ERROR),
        (logging.CRITICAL, SeverityNumber.FATAL),
        (5, SeverityNumber.TRACE),
    ],
)
def test_stdlib_levels_map_to_otel_severities(level: int, severity: SeverityNumber) -> None:
    assert severity_of(level) is severity
