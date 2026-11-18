from collections.abc import Sequence
from typing import Any

from opentelemetry.sdk.trace import Event, ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.semconv.attributes.url_attributes import URL_PATH
from opentelemetry.trace import Link, Status
from opentelemetry.util.types import Attributes

from pyobservability.exporters.health import record_export_failure
from pyobservability.security import Redactor
from pyobservability.security.paths import templated_path


class RedactingSpanExporter(SpanExporter):
    """Ended spans are immutable, so redaction rebuilds each span just before export."""

    def __init__(
        self, exporter: SpanExporter, redactor: Redactor, *, template_paths: bool = True
    ) -> None:
        self._exporter = exporter
        self._redactor = redactor
        self._template_paths = template_paths

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        result = self._exporter.export([self._redacted(span) for span in spans])
        if result is not SpanExportResult.SUCCESS:
            record_export_failure("traces")
        return result

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self._exporter.force_flush(timeout_millis)

    def shutdown(self) -> None:
        self._exporter.shutdown()

    def _redacted(self, span: ReadableSpan) -> ReadableSpan:
        return ReadableSpan(
            name=self._redactor.redact_text(span.name),
            context=span.context,
            parent=span.parent,
            resource=span.resource,
            attributes=self._attributes(span.attributes),
            events=[self._event(event) for event in span.events],
            links=[Link(link.context, self._attributes(link.attributes)) for link in span.links],
            kind=span.kind,
            status=self._status(span.status),
            start_time=span.start_time,
            end_time=span.end_time,
            instrumentation_scope=span.instrumentation_scope,
        )

    def _attributes(self, attributes: Attributes) -> dict[str, Any]:
        redacted = self._redactor.redact_fields(attributes or {})
        path = redacted.get(URL_PATH)
        if self._template_paths and isinstance(path, str):
            redacted[URL_PATH] = templated_path(path)
        return redacted

    def _event(self, event: Event) -> Event:
        return Event(
            name=event.name,
            attributes=self._attributes(event.attributes),
            timestamp=event.timestamp,
        )

    def _status(self, status: Status) -> Status:
        if status.description is None:
            return status
        return Status(status.status_code, self._redactor.redact_text(status.description))
