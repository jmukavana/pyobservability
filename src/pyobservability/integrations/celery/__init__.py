"""Celery integration: `instrument_celery()` once, in both the publisher and the worker.

Publishing carries the trace, request ID and tenant in task headers; each task run then gets a
CONSUMER span, the same context, and `messaging.process.duration`. Task arguments are never
recorded.
"""

import threading
import time
from contextlib import ExitStack
from dataclasses import dataclass, field
from typing import Any

from opentelemetry import trace
from opentelemetry.semconv._incubating.attributes.messaging_attributes import (
    MESSAGING_DESTINATION_NAME,
    MESSAGING_MESSAGE_ID,
    MESSAGING_OPERATION_NAME,
    MESSAGING_SYSTEM,
)
from opentelemetry.semconv._incubating.metrics.messaging_metrics import (
    MESSAGING_PROCESS_DURATION,
)
from opentelemetry.semconv.attributes.error_attributes import ERROR_TYPE
from opentelemetry.trace import Span, SpanKind, Status, StatusCode

from pyobservability import context
from pyobservability.context.correlation import REQUEST_ID, TENANT_ID, USER_ID, new_request_id
from pyobservability.exceptions import MissingExtraError
from pyobservability.metrics import get_meter
from pyobservability.tracing import context_from_headers, get_tracer, inject_trace_headers

try:
    from celery import Task, signals
except ImportError as error:
    raise MissingExtraError("celery", "the Celery integration") from error

CELERY = "celery"
PROCESS = "process"
HEADER_PREFIX = "pyobservability_"
PROPAGATED_CONTEXT = (REQUEST_ID, TENANT_ID, USER_ID)
SUCCESS_STATE = "SUCCESS"

_tracer = get_tracer("pyobservability.celery")
_duration = get_meter("pyobservability.celery").create_histogram(
    MESSAGING_PROCESS_DURATION, unit="s", description="Duration of Celery task processing"
)


@dataclass(slots=True)
class _RunningTask:
    span: Span
    queue: str
    started_at: float
    scope: ExitStack = field(default_factory=ExitStack)
    error: BaseException | None = None


class _Runs:
    """Task runs by ID; thread and gevent pools run several in one process."""

    def __init__(self) -> None:
        self._runs: dict[str, _RunningTask] = {}
        self._lock = threading.Lock()

    def add(self, task_id: str, run: _RunningTask) -> None:
        with self._lock:
            self._runs[task_id] = run

    def get(self, task_id: str) -> _RunningTask | None:
        with self._lock:
            return self._runs.get(task_id)

    def pop(self, task_id: str) -> _RunningTask | None:
        with self._lock:
            return self._runs.pop(task_id, None)


_runs = _Runs()


def instrument_celery() -> None:
    """Connects the publish and run signals; Celery's `dispatch_uid` makes repeats no-ops."""
    connections = (
        (signals.before_task_publish, _inject_into_headers),
        (signals.task_prerun, _start_run),
        (signals.task_failure, _record_failure),
        (signals.task_postrun, _finish_run),
    )
    for signal, receiver in connections:
        signal.connect(receiver, weak=False, dispatch_uid=f"pyobservability.{receiver.__name__}")


def _inject_into_headers(headers: dict[str, Any] | None = None, **_: Any) -> None:
    if headers is None:
        return
    inject_trace_headers(headers)
    for key in PROPAGATED_CONTEXT:
        if (value := context.get(key)) is not None:
            headers[HEADER_PREFIX + key] = str(value)


def _start_run(task_id: str, task: Task, **_: Any) -> None:
    request = task.request
    carrier = {name: value for name, value in vars(request).items() if isinstance(value, str)}
    queue = (getattr(request, "delivery_info", None) or {}).get("routing_key") or CELERY
    run = _RunningTask(
        span=_tracer.start_span(
            f"{PROCESS} {task.name}",
            context=context_from_headers(carrier),
            kind=SpanKind.CONSUMER,
            attributes={
                MESSAGING_SYSTEM: CELERY,
                MESSAGING_OPERATION_NAME: PROCESS,
                MESSAGING_DESTINATION_NAME: queue,
                MESSAGING_MESSAGE_ID: task_id,
                "celery.task.name": task.name,
            },
        ),
        queue=queue,
        started_at=time.perf_counter(),
    )
    run.scope.enter_context(trace.use_span(run.span, end_on_exit=False))
    run.scope.enter_context(context.scope(**_propagated_context(carrier)))
    _runs.add(task_id, run)


def _propagated_context(carrier: dict[str, str]) -> dict[str, str]:
    values = {
        key: carrier[HEADER_PREFIX + key]
        for key in PROPAGATED_CONTEXT
        if HEADER_PREFIX + key in carrier
    }
    values.setdefault(REQUEST_ID, new_request_id())
    return values


def _record_failure(task_id: str, exception: BaseException, **_: Any) -> None:
    if (run := _runs.get(task_id)) is not None:
        run.error = exception


def _finish_run(task_id: str, task: Task, state: str | None = None, **_: Any) -> None:
    run = _runs.pop(task_id)
    if run is None:
        return
    labels = {MESSAGING_SYSTEM: CELERY, MESSAGING_OPERATION_NAME: PROCESS}
    labels[MESSAGING_DESTINATION_NAME] = run.queue
    if run.error is not None:
        labels[ERROR_TYPE] = type(run.error).__qualname__
        run.span.record_exception(run.error)
        run.span.set_status(Status(StatusCode.ERROR))
    elif state is not None and state != SUCCESS_STATE:
        labels[ERROR_TYPE] = state
    if ERROR_TYPE in labels:
        run.span.set_attribute(ERROR_TYPE, labels[ERROR_TYPE])
    run.span.end()
    _duration.record(time.perf_counter() - run.started_at, labels)
    run.scope.close()


__all__ = ["instrument_celery"]
