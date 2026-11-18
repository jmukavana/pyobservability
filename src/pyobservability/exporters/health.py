from collections.abc import Sequence
from typing import Literal

from opentelemetry import metrics
from opentelemetry.sdk._logs import ReadableLogRecord
from opentelemetry.sdk._logs.export import LogRecordExporter, LogRecordExportResult

EXPORT_ERRORS_METRIC = "pyobs.export.errors"
Signal = Literal["traces", "logs"]

_export_errors = metrics.get_meter("pyobservability").create_counter(
    EXPORT_ERRORS_METRIC, unit="{batch}", description="Telemetry batches an exporter failed to send"
)


def record_export_failure(signal: Signal) -> None:
    _export_errors.add(1, {"signal": signal})


class FailureCountingLogExporter(LogRecordExporter):
    def __init__(self, exporter: LogRecordExporter) -> None:
        self._exporter = exporter

    def export(self, batch: Sequence[ReadableLogRecord]) -> LogRecordExportResult:
        result = self._exporter.export(batch)
        if result is not LogRecordExportResult.SUCCESS:
            record_export_failure("logs")
        return result

    def shutdown(self) -> None:
        self._exporter.shutdown()  # type: ignore[no-untyped-call]  # untyped in the OTel SDK

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self._exporter.force_flush(timeout_millis)
