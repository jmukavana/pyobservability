import threading
import time
from collections import deque
from collections.abc import Sequence

from pyobservability.audit.delivery import Admission, Delivery, DeliveryInterrupted
from pyobservability.audit.outcomes import DeliveryOutcomes
from pyobservability.audit.schemas import AuditEvent
from pyobservability.audit.wal import WriteAheadLog

UNDELIVERED_AT_SHUTDOWN = "process shut down before delivery"
STILL_EXPORTING_AT_SHUTDOWN = "exporter still busy at shutdown; may also have been delivered"


class Dispatcher:
    """Background delivery: one thread, batches in arrival order."""

    def __init__(
        self,
        delivery: Delivery,
        admission: Admission,
        outcomes: DeliveryOutcomes,
        wal: WriteAheadLog | None,
        *,
        batch_size: int,
    ) -> None:
        self._delivery = delivery
        self._admission = admission
        self._outcomes = outcomes
        self._batch_size = batch_size
        self._stop = threading.Event()
        self._wal = wal
        self._queue: deque[AuditEvent] = deque()
        self._has_work = threading.Condition()
        self._draining = False
        self._in_progress: list[AuditEvent] = []
        self._abandoned = False
        self._thread = threading.Thread(target=self._run, name="pyobservability-audit", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def submit(self, event: AuditEvent) -> None:
        with self._has_work:
            self._queue.append(event)
            self._has_work.notify()

    def shutdown(self, timeout_s: float) -> None:
        deadline = time.monotonic() + timeout_s
        with self._has_work:
            self._draining = True
            self._has_work.notify()
        self._admission.wait_until_idle(max(deadline - time.monotonic(), 0.0))
        self._stop.set()
        with self._has_work:
            self._has_work.notify_all()
        self._thread.join(max(deadline - time.monotonic(), 0.0))
        self._dead_letter_memory_only(self._take_all(), UNDELIVERED_AT_SHUTDOWN)
        if self._thread.is_alive():
            self._abandon_in_progress()

    def _run(self) -> None:
        while (batch := self._next_batch()) is not None:
            try:
                self._process(batch)
            except DeliveryInterrupted:
                self._dead_letter_memory_only(batch, UNDELIVERED_AT_SHUTDOWN)
                return
            finally:
                with self._has_work:
                    self._in_progress = []

    def _abandon_in_progress(self) -> None:
        """A hung exporter must not take memory-only events down with the daemon thread."""
        with self._has_work:
            batch, self._in_progress = self._in_progress, []
            self._abandoned = True
        self._dead_letter_memory_only(batch, STILL_EXPORTING_AT_SHUTDOWN)

    def _next_batch(self) -> list[AuditEvent] | None:
        with self._has_work:
            self._has_work.wait_for(lambda: self._queue or self._draining or self._stop.is_set())
            if self._stop.is_set() or (self._draining and not self._queue):
                return None
            batch = [self._queue.popleft() for _ in range(min(self._batch_size, len(self._queue)))]
            self._in_progress = batch
            return batch

    def _process(self, batch: Sequence[AuditEvent]) -> None:
        undelivered = self._delivery.deliver(batch, self._stop.wait)
        if self._abandoned:
            return
        self._outcomes.settle(batch, undelivered)
        self._finish(batch)

    def _finish(self, batch: Sequence[AuditEvent]) -> None:
        if self._wal is not None:
            self._wal.acknowledge(event.event_id for event in batch)
        self._admission.release(len(batch))

    def _dead_letter_memory_only(self, events: Sequence[AuditEvent], reason: str) -> None:
        """Events in the write-ahead log survive a shutdown; memory-only ones would vanish."""
        if not events:
            return
        if self._wal is None:
            self._outcomes.dead_lettered(events, reason)
        self._admission.release(len(events))

    def _take_all(self) -> list[AuditEvent]:
        with self._has_work:
            remaining = list(self._queue)
            self._queue.clear()
            return remaining
