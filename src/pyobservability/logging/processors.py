import os
import socket
import sys
import traceback
from typing import Any

from structlog.typing import EventDict, WrappedLogger

from pyobservability.config import ObservabilityConfig
from pyobservability.context import current_ids, snapshot
from pyobservability.security import Redactor

LEADING_FIELDS = ("timestamp", "level", "message", "logger")
TRUSTED_FIELDS = frozenset(
    {*LEADING_FIELDS, "service", "environment", "version", "host", "pid", "trace_id", "span_id"}
)


def uppercase_level(_: WrappedLogger, __: str, event: EventDict) -> EventDict:
    event["level"] = str(event.get("level", "info")).upper()
    return event


def add_context(_: WrappedLogger, __: str, event: EventDict) -> EventDict:
    return {**snapshot(), **event}


def add_correlation_ids(_: WrappedLogger, __: str, event: EventDict) -> EventDict:
    event.update(current_ids())
    return event


class ServiceMetadata:
    def __init__(self, config: ObservabilityConfig) -> None:
        self._fields = {
            "service": config.service_name,
            "environment": config.environment,
            "version": config.service_version,
            "host": socket.gethostname(),
        }

    def __call__(self, _: WrappedLogger, __: str, event: EventDict) -> EventDict:
        event.update(self._fields)
        event["pid"] = os.getpid()
        return event


def structure_exception(_: WrappedLogger, __: str, event: EventDict) -> EventDict:
    exception = _exception_from(event.pop("exc_info", None))
    if exception is not None:
        event["exception"] = {
            "type": type(exception).__qualname__,
            "message": str(exception),
            "stacktrace": "".join(traceback.format_exception(exception)),
        }
    return event


def _exception_from(exc_info: Any) -> BaseException | None:
    if isinstance(exc_info, BaseException):
        return exc_info
    if isinstance(exc_info, tuple):
        return next((item for item in exc_info if isinstance(item, BaseException)), None)
    if exc_info:
        return sys.exc_info()[1]
    return None


class RedactUntrustedFields:
    """Redacts everything except fields this SDK produced itself."""

    def __init__(self, redactor: Redactor) -> None:
        self._redactor = redactor

    def __call__(self, _: WrappedLogger, __: str, event: EventDict) -> EventDict:
        trusted = {key: value for key, value in event.items() if key in TRUSTED_FIELDS}
        untrusted = {key: value for key, value in event.items() if key not in TRUSTED_FIELDS}
        if "message" in trusted:
            trusted["message"] = self._redactor.redact_text(str(trusted["message"]))
        return {**trusted, **self._redactor.redact_fields(untrusted)}


def leading_fields_first(_: WrappedLogger, __: str, event: EventDict) -> EventDict:
    leading = {key: event[key] for key in LEADING_FIELDS if key in event}
    return {**leading, **event}
