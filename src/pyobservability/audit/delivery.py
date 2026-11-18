import json
import logging
import os
import random
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from uuid import UUID

from pyobservability.audit.exporters import AuditExporter
from pyobservability.audit.models import ExportResult, Failure
from pyobservability.audit.schemas import AuditEvent
from pyobservability.config import RetryConfig
from pyobservability.exceptions import AuditDurabilityError

DEAD_LETTER_FILE = "dead-letter.jsonl"
OWNER_READ_WRITE = 0o600

_logger = logging.getLogger("pyobservability.audit")

Wait = Callable[[float], bool]
"""Sleeps for the given seconds; returns True when delivery should stop instead."""


class DeliveryInterrupted(Exception):
    pass


class RetrySchedule:
    def __init__(self, config: RetryConfig, jitter_source: Callable[[], float] = random.random):
        self._config = config
        self._jitter_source = jitter_source

    @property
    def max_attempts(self) -> int:
        return self._config.max_attempts

    def delay_after(self, attempt: int) -> float:
        exponential = self._config.initial_backoff_s * self._config.multiplier ** (attempt - 1)
        capped = min(exponential, self._config.max_backoff_s)
        spread = self._config.jitter * (2 * self._jitter_source() - 1)
        return max(capped * (1 + spread), 0.0)


@dataclass(frozen=True, slots=True)
class ConfiguredExporter:
    exporter: AuditExporter
    required: bool


class DeadLetterSink(Protocol):
    def write(self, events: Sequence[AuditEvent], reason: str) -> None: ...


class LogDeadLetters:
    def write(self, events: Sequence[AuditEvent], reason: str) -> None:
        for event in events:
            _logger.error(
                "audit event could not be delivered: %s %s",
                reason,
                event.model_dump_json(),
            )


class FileDeadLetters:
    def __init__(self, directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        self._path = directory / DEAD_LETTER_FILE
        self._lock = threading.Lock()

    def write(self, events: Sequence[AuditEvent], reason: str) -> None:
        lines = "".join(
            json.dumps({"reason": reason, "event": event.model_dump(mode="json")}) + "\n"
            for event in events
        )
        with self._lock:
            descriptor = os.open(
                self._path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, OWNER_READ_WRITE
            )
            try:
                os.write(descriptor, lines.encode())
                os.fsync(descriptor)
            finally:
                os.close(descriptor)


class Delivery:
    """Delivers a batch to every exporter, retrying retryable failures with bounded backoff."""

    def __init__(self, exporters: Sequence[ConfiguredExporter], schedule: RetrySchedule) -> None:
        self.exporters = tuple(exporters)
        self._schedule = schedule

    def deliver(
        self, events: Sequence[AuditEvent], wait: Wait
    ) -> dict[ConfiguredExporter, dict[UUID, str]]:
        """Returns, per exporter, the events it never accepted and why."""
        return {target: self.deliver_to(target, events, wait) for target in self.exporters}

    def deliver_to(
        self, target: ConfiguredExporter, events: Sequence[AuditEvent], wait: Wait
    ) -> dict[UUID, str]:
        exporter = target.exporter
        undelivered: dict[UUID, str] = {}
        remaining = list(events)
        failures: Mapping[UUID, Failure] = {}
        for attempt in range(1, self._schedule.max_attempts + 1):
            failures = _export_safely(exporter, remaining)
            undelivered.update(
                (event_id, failure.reason)
                for event_id, failure in failures.items()
                if not failure.retryable
            )
            remaining = [event for event in remaining if _is_retryable(failures, event)]
            if not remaining:
                return undelivered
            if attempt < self._schedule.max_attempts and wait(self._schedule.delay_after(attempt)):
                raise DeliveryInterrupted
        reasons = {
            event.event_id: f"gave up: {failures[event.event_id].reason}" for event in remaining
        }
        return {**undelivered, **reasons}


def _export_safely(exporter: AuditExporter, events: Sequence[AuditEvent]) -> Mapping[UUID, Failure]:
    try:
        return exporter.export(events).failures
    except Exception as error:
        reason = f"{exporter.name} raised {type(error).__name__}: {error}"
        return ExportResult.all_failed(events, reason).failures


def _is_retryable(failures: Mapping[UUID, Failure], event: AuditEvent) -> bool:
    failure = failures.get(event.event_id)
    return failure is not None and failure.retryable


class Admission:
    """Bounds events in flight; full means block or refuse, never drop."""

    def __init__(self, capacity: int, *, blocks: bool, timeout_s: float) -> None:
        self._capacity = capacity
        self._blocks = blocks
        self._timeout_s = timeout_s
        self._in_flight = 0
        self._condition = threading.Condition()

    @property
    def in_flight(self) -> int:
        return self._in_flight

    def admit(self) -> None:
        with self._condition:
            if self._in_flight >= self._capacity and not self._blocks:
                raise AuditDurabilityError("audit queue is full")
            if not self._condition.wait_for(self._has_room, self._timeout_s):
                raise AuditDurabilityError(
                    f"audit queue stayed full for {self._timeout_s}s; the event was not recorded"
                )
            self._in_flight += 1

    def admit_recovered(self, count: int) -> None:
        with self._condition:
            self._in_flight += count

    def release(self, count: int) -> None:
        with self._condition:
            self._in_flight -= count
            self._condition.notify_all()

    def wait_until_idle(self, timeout_s: float) -> bool:
        with self._condition:
            return self._condition.wait_for(lambda: self._in_flight == 0, timeout_s)

    def _has_room(self) -> bool:
        return self._in_flight < self._capacity


def deadline_wait(deadline: float) -> Wait:
    """For inline delivery: never sleeps past the caller's deadline."""

    def wait(seconds: float) -> bool:
        remaining = deadline - time.monotonic()
        if remaining <= seconds:
            return True
        time.sleep(seconds)
        return False

    return wait
