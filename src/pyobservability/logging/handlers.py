import logging
import sys
from collections.abc import Mapping, Sequence
from logging.handlers import WatchedFileHandler
from typing import Any

from opentelemetry import context as otel_context
from opentelemetry._logs import SeverityNumber
from opentelemetry.sdk._logs import LoggerProvider

from pyobservability.config import LoggingConfig
from pyobservability.logging.processors import RedactUntrustedFields, structure_exception
from pyobservability.security import Redactor

_MARKER = "_pyobservability_handler"


class OnlyStructlogRecords(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return _from_structlog(record)


NANOS_PER_SECOND = 1_000_000_000

_SEVERITIES = (
    (logging.CRITICAL, SeverityNumber.FATAL),
    (logging.ERROR, SeverityNumber.ERROR),
    (logging.WARNING, SeverityNumber.WARN),
    (logging.INFO, SeverityNumber.INFO),
    (logging.DEBUG, SeverityNumber.DEBUG),
)


def severity_of(level: int) -> SeverityNumber:
    return next(
        (severity for threshold, severity in _SEVERITIES if level >= threshold),
        SeverityNumber.TRACE,
    )


class RedactedOtelLogHandler(logging.Handler):
    """Exports records as OpenTelemetry logs whose body is always a redacted event dict."""

    def __init__(self, logger_provider: LoggerProvider, redactor: Redactor) -> None:
        super().__init__()
        self._logger_provider = logger_provider
        self._redact = RedactUntrustedFields(redactor)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._logger_provider.get_logger(record.name).emit(
                timestamp=int(record.created * NANOS_PER_SECOND),
                context=otel_context.get_current(),
                severity_number=severity_of(record.levelno),
                severity_text=record.levelname,
                body=self._body(record),
            )
        except Exception:
            self.handleError(record)

    def _body(self, record: logging.LogRecord) -> Mapping[str, Any]:
        if _from_structlog(record) and isinstance(record.msg, dict):
            return record.msg
        event = {"message": record.getMessage(), "exc_info": record.exc_info}
        return self._redact(None, record.levelname, structure_exception(None, "", event))


def _from_structlog(record: logging.LogRecord) -> bool:
    return hasattr(record, "_logger")


def build_handler(config: LoggingConfig) -> logging.Handler:
    handler: logging.Handler
    match config.output:
        case "stdout":
            handler = logging.StreamHandler(sys.stdout)
        case "stderr":
            handler = logging.StreamHandler(sys.stderr)
        case "file":
            handler = WatchedFileHandler(str(config.file_path), encoding="utf-8")
    return handler


def mark_ours(handler: logging.Handler, config: LoggingConfig) -> logging.Handler:
    if not config.capture_stdlib:
        handler.addFilter(OnlyStructlogRecords())
    setattr(handler, _MARKER, True)
    return handler


def is_ours(handler: logging.Handler) -> bool:
    return getattr(handler, _MARKER, False)


def replace_our_handlers(logger: logging.Logger, handlers: Sequence[logging.Handler]) -> None:
    remove_our_handlers(logger)
    for handler in handlers:
        logger.addHandler(handler)


def remove_our_handlers(logger: logging.Logger) -> None:
    for existing in [handler for handler in logger.handlers if is_ours(handler)]:
        logger.removeHandler(existing)
        existing.close()
