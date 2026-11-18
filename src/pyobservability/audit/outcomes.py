import logging
from collections.abc import Callable, Mapping, Sequence
from uuid import UUID

from opentelemetry import metrics

from pyobservability.audit.delivery import ConfiguredExporter, DeadLetterSink
from pyobservability.audit.schemas import AuditEvent, utc_now

FailureCallback = Callable[[AuditEvent, str], None]

_logger = logging.getLogger("pyobservability.audit")
AUDIT_METER = "pyobservability.audit"
_meter = metrics.get_meter(AUDIT_METER)
_recorded = _meter.create_counter("pyobs.audit.events.recorded", unit="{event}")
_delivered = _meter.create_counter("pyobs.audit.events.delivered", unit="{event}")
_failed = _meter.create_counter("pyobs.audit.events.failed", unit="{event}")
_latency = _meter.create_histogram("pyobs.audit.delivery.latency", unit="s")


class DeliveryOutcomes:
    """Where delivery results go: dead letters, the user's callback and SDK self-metrics."""

    def __init__(self, dead_letters: DeadLetterSink, on_failure: FailureCallback | None) -> None:
        self._dead_letters = dead_letters
        self._on_failure = on_failure

    def recorded(self, event: AuditEvent) -> None:
        _recorded.add(1, {"category": event.category, "outcome": event.outcome})

    def delivered(self, events: Sequence[AuditEvent]) -> None:
        now = utc_now()
        _delivered.add(len(events))
        for event in events:
            _latency.record((now - event.recorded_at).total_seconds())

    def settle(
        self,
        events: Sequence[AuditEvent],
        undelivered: Mapping[ConfiguredExporter, Mapping[UUID, str]],
    ) -> None:
        """Dead-letters what each exporter refused and counts the rest as delivered."""
        failed: set[UUID] = set()
        for target, reasons in undelivered.items():
            for event in events:
                if event.event_id in reasons:
                    failed.add(event.event_id)
                    reason = f"{target.exporter.name}: {reasons[event.event_id]}"
                    self.dead_lettered([event], reason)
        self.delivered([event for event in events if event.event_id not in failed])

    def dead_lettered(self, events: Sequence[AuditEvent], reason: str) -> None:
        self._dead_letters.write(events, reason)
        _failed.add(len(events), {"reason": reason.split(":", 1)[0]})
        for event in events:
            self._notify(event, reason)

    def _notify(self, event: AuditEvent, reason: str) -> None:
        if self._on_failure is None:
            return
        try:
            self._on_failure(event, reason)
        except Exception:
            _logger.exception("on_delivery_failure callback raised for event %s", event.event_id)
