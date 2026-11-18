import sys
import threading
from collections.abc import Sequence
from typing import TextIO

from pyobservability.audit.models import ExportResult
from pyobservability.audit.schemas import AuditEvent


class ConsoleAuditExporter:
    """JSON lines on stdout; for development, not a durable system of record."""

    name = "console"

    def __init__(self, stream: TextIO | None = None) -> None:
        self._stream = stream
        self._lock = threading.Lock()

    def export(self, events: Sequence[AuditEvent]) -> ExportResult:
        stream = self._stream or sys.stdout
        lines = "".join(event.model_dump_json() + "\n" for event in events)
        with self._lock:
            stream.write(lines)
            stream.flush()
        return ExportResult.success()

    def shutdown(self) -> None:
        pass
