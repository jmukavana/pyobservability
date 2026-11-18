import os
import threading
from collections.abc import Sequence
from pathlib import Path

from pyobservability.audit.models import ExportResult
from pyobservability.audit.schemas import AuditEvent

OWNER_READ_WRITE = 0o600


class JsonLinesAuditExporter:
    """Appends events to a JSON-lines file readable only by its owner."""

    name = "file"

    def __init__(self, path: str | Path, fsync: bool = True) -> None:
        self._path = Path(path)
        self._fsync = fsync
        self._lock = threading.Lock()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._descriptor = os.open(
            self._path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, OWNER_READ_WRITE
        )

    def export(self, events: Sequence[AuditEvent]) -> ExportResult:
        payload = "".join(event.model_dump_json() + "\n" for event in events).encode()
        try:
            with self._lock:
                os.write(self._descriptor, payload)
                if self._fsync:
                    os.fsync(self._descriptor)
        except OSError as error:
            return ExportResult.all_failed(events, f"cannot write {self._path}: {error}")
        return ExportResult.success()

    def shutdown(self) -> None:
        with self._lock:
            os.close(self._descriptor)
