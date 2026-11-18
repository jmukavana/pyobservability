"""SQLAlchemy integration.

`instrument_engine(engine)` traces statements (never their bound parameters) and records
`db.client.operation.duration`. Importing this module also lets `audit.record(transaction=...)`
write to the outbox through a SQLAlchemy `Session` or `Connection`.
"""

import time
from dataclasses import dataclass
from typing import Any

from opentelemetry.semconv.attributes.db_attributes import (
    DB_NAMESPACE,
    DB_OPERATION_NAME,
    DB_QUERY_TEXT,
    DB_SYSTEM_NAME,
)
from opentelemetry.semconv.attributes.error_attributes import ERROR_TYPE
from opentelemetry.semconv.attributes.server_attributes import SERVER_ADDRESS, SERVER_PORT
from opentelemetry.semconv.metrics.db_metrics import DB_CLIENT_OPERATION_DURATION
from opentelemetry.trace import Span, SpanKind, Status, StatusCode

from pyobservability.exceptions import MissingExtraError
from pyobservability.logging import get_logger
from pyobservability.metrics import get_meter
from pyobservability.tracing import get_tracer

try:
    from sqlalchemy import Connection, Engine, event, text
    from sqlalchemy.engine.interfaces import ExceptionContext, ExecutionContext
    from sqlalchemy.orm import Session
except ImportError as error:
    raise MissingExtraError("sqlalchemy", "the SQLAlchemy integration") from error

MAX_QUERY_TEXT_LENGTH = 2048
SLOW_QUERY_LOGGER = "pyobservability.sqlalchemy"
_IN_FLIGHT = "_pyobservability_statement"

_tracer = get_tracer("pyobservability.sqlalchemy")
_duration = get_meter("pyobservability.sqlalchemy").create_histogram(
    DB_CLIENT_OPERATION_DURATION, unit="s", description="Duration of database client operations"
)


@dataclass(slots=True)
class _Statement:
    span: Span
    operation: str
    system: str
    started_at: float


@dataclass(frozen=True, slots=True)
class _EngineListeners:
    slow_query_threshold_s: float | None

    def before(self, **event_arguments: Any) -> None:
        connection: Connection = event_arguments["conn"]
        statement: str = event_arguments["statement"]
        context: ExecutionContext = event_arguments["context"]
        operation = _operation_of(statement)
        system = connection.dialect.name
        url = connection.engine.url
        attributes: dict[str, Any] = {
            DB_SYSTEM_NAME: system,
            DB_OPERATION_NAME: operation,
            DB_QUERY_TEXT: statement[:MAX_QUERY_TEXT_LENGTH],
        }
        if url.database:
            attributes[DB_NAMESPACE] = url.database
        if url.host:
            attributes[SERVER_ADDRESS] = url.host
        if url.port:
            attributes[SERVER_PORT] = url.port
        span = _tracer.start_span(
            f"{operation} {url.database}" if url.database else operation,
            kind=SpanKind.CLIENT,
            attributes=attributes,
        )
        setattr(context, _IN_FLIGHT, _Statement(span, operation, system, time.perf_counter()))

    def after(self, **event_arguments: Any) -> None:
        self._finish(event_arguments["context"], None)

    def failed(self, exception_context: ExceptionContext) -> None:
        if exception_context.execution_context is not None:
            self._finish(exception_context.execution_context, exception_context.original_exception)

    def _finish(self, context: ExecutionContext, error: BaseException | None) -> None:
        statement: _Statement | None = getattr(context, _IN_FLIGHT, None)
        if statement is None:
            return
        delattr(context, _IN_FLIGHT)
        duration_s = time.perf_counter() - statement.started_at
        labels = {DB_SYSTEM_NAME: statement.system, DB_OPERATION_NAME: statement.operation}
        if error is not None:
            labels[ERROR_TYPE] = type(error).__qualname__
            statement.span.record_exception(error)
            statement.span.set_status(Status(StatusCode.ERROR))
            statement.span.set_attribute(ERROR_TYPE, type(error).__qualname__)
        statement.span.end()
        _duration.record(duration_s, labels)
        self._warn_if_slow(statement, duration_s)

    def _warn_if_slow(self, statement: _Statement, duration_s: float) -> None:
        threshold = self.slow_query_threshold_s
        if threshold is not None and duration_s >= threshold:
            get_logger(SLOW_QUERY_LOGGER).warning(
                "slow database query",
                operation=statement.operation,
                db_system=statement.system,
                duration_ms=round(duration_s * 1000, 3),
            )


_INSTRUMENTED = "pyobservability_listeners"


def instrument_engine(engine: Engine, *, slow_query_threshold_s: float | None = None) -> Engine:
    """Adds statement spans and duration metrics; instrumenting twice is harmless."""
    if _INSTRUMENTED in engine.__dict__:
        return engine
    listeners = _EngineListeners(slow_query_threshold_s)
    event.listen(engine, "before_cursor_execute", listeners.before, named=True)
    event.listen(engine, "after_cursor_execute", listeners.after, named=True)
    event.listen(engine, "handle_error", listeners.failed)
    engine.__dict__[_INSTRUMENTED] = listeners
    return engine


def _operation_of(statement: str) -> str:
    words = statement.split(None, 1)
    return words[0].upper() if words else "QUERY"


def _write_outbox_row(transaction: Session | Connection, row: Any) -> None:
    transaction.execute(text(row.named_insert), {"event_id": row.event_id, "payload": row.payload})


def _register_outbox_writers() -> None:
    """The outbox lives in the `postgres` extra; without it there is nothing to register."""
    try:
        from pyobservability.audit.outbox import write_outbox_row  # noqa: PLC0415 - optional
    except MissingExtraError:
        return
    write_outbox_row.register(Session, _write_outbox_row)
    write_outbox_row.register(Connection, _write_outbox_row)


_register_outbox_writers()

__all__ = ["instrument_engine"]
