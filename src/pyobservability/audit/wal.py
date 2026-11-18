import json
import os
import threading
from collections.abc import Iterable
from pathlib import Path
from typing import Any
from uuid import UUID

from pydantic import ValidationError

from pyobservability.audit.schemas import AuditEvent
from pyobservability.exceptions import ConfigurationError

try:
    import fcntl
except ImportError:  # Windows: single-process use of a spool directory is not enforced.
    fcntl = None  # type: ignore[assignment]

LOG_FILE = "audit.wal"
LOCK_FILE = "audit.lock"
OWNER_ONLY_DIRECTORY = 0o700
OWNER_READ_WRITE = 0o600


class WriteAheadLog:
    """Durable record of events not yet delivered.

    Appends are fsynced before `record()` returns. Acknowledgements are not: after a crash an
    acknowledged event may be delivered again, which exporters tolerate through `event_id`.
    """

    def __init__(self, directory: Path, *, fsync: bool) -> None:
        self._directory = directory
        self._fsync = fsync
        self._lock = threading.Lock()
        self._pending: set[UUID] = set()
        self._descriptor: int | None = None
        self._lock_descriptor: int | None = None

    def open(self) -> list[AuditEvent]:
        """Takes ownership of the directory and returns events left undelivered by a crash."""
        self._directory.mkdir(parents=True, exist_ok=True, mode=OWNER_ONLY_DIRECTORY)
        self._acquire_directory_lock()
        pending = self._recover()
        self._rewrite(pending)
        self._descriptor = os.open(
            self._log_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, OWNER_READ_WRITE
        )
        self._pending = {event.event_id for event in pending}
        return pending

    def append(self, event: AuditEvent) -> None:
        line = json.dumps({"append": event.model_dump(mode="json")}) + "\n"
        with self._lock:
            os.write(self._open_descriptor(), line.encode())
            if self._fsync:
                os.fsync(self._open_descriptor())
            self._pending.add(event.event_id)

    def acknowledge(self, event_ids: Iterable[UUID]) -> None:
        ids = [str(event_id) for event_id in event_ids]
        with self._lock:
            self._pending.difference_update(UUID(event_id) for event_id in ids)
            if self._pending:
                os.write(self._open_descriptor(), (json.dumps({"ack": ids}) + "\n").encode())
            else:
                os.ftruncate(self._open_descriptor(), 0)

    def close(self) -> None:
        with self._lock:
            if self._descriptor is not None:
                os.close(self._descriptor)
                self._descriptor = None
            if self._lock_descriptor is not None:
                os.close(self._lock_descriptor)
                self._lock_descriptor = None

    @property
    def _log_path(self) -> Path:
        return self._directory / LOG_FILE

    def _open_descriptor(self) -> int:
        if self._descriptor is None:
            raise OSError("write-ahead log is closed")
        return self._descriptor

    def _acquire_directory_lock(self) -> None:
        self._lock_descriptor = os.open(
            self._directory / LOCK_FILE, os.O_RDWR | os.O_CREAT, OWNER_READ_WRITE
        )
        if fcntl is None:
            return
        try:
            fcntl.flock(self._lock_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(self._lock_descriptor)
            self._lock_descriptor = None
            raise ConfigurationError(
                f"audit spool {self._directory} is in use by another process"
            ) from None

    def _recover(self) -> list[AuditEvent]:
        if not self._log_path.exists():
            return []
        pending: dict[UUID, AuditEvent] = {}
        for line in self._log_path.read_text(encoding="utf-8").splitlines():
            entry = _parsed(line)
            if "append" in entry:
                event = _event_from(entry["append"])
                if event is not None:
                    pending[event.event_id] = event
            for event_id in entry.get("ack", ()):
                pending.pop(UUID(event_id), None)
        return list(pending.values())

    def _rewrite(self, pending: list[AuditEvent]) -> None:
        temporary = self._log_path.with_suffix(".compacting")
        lines = "".join(
            json.dumps({"append": event.model_dump(mode="json")}) + "\n" for event in pending
        )
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, OWNER_READ_WRITE)
        try:
            os.write(descriptor, lines.encode())
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, self._log_path)


def _parsed(line: str) -> dict[str, Any]:
    """A crash mid-write leaves a torn final line; it never made `record()` return."""
    try:
        entry = json.loads(line)
    except json.JSONDecodeError:
        return {}
    return entry if isinstance(entry, dict) else {}


def _event_from(payload: Any) -> AuditEvent | None:
    try:
        return AuditEvent.model_validate(payload)
    except ValidationError:
        return None
