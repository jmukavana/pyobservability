import logging
import sys
from typing import TextIO

import structlog
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from structlog.typing import Processor

from pyobservability.config import ObservabilityConfig, SecurityConfig
from pyobservability.core.resource import build_resource
from pyobservability.core.timeouts import to_millis
from pyobservability.exporters import log_exporters
from pyobservability.exporters.health import FailureCountingLogExporter
from pyobservability.logging.formatters import renderer_for
from pyobservability.logging.handlers import (
    RedactedOtelLogHandler,
    build_handler,
    mark_ours,
    remove_our_handlers,
    replace_our_handlers,
)
from pyobservability.logging.processors import (
    RedactUntrustedFields,
    ServiceMetadata,
    add_context,
    add_correlation_ids,
    leading_fields_first,
    structure_exception,
    uppercase_level,
)
from pyobservability.security import Redactor

_CALLSITE = structlog.processors.CallsiteParameterAdder(
    {
        structlog.processors.CallsiteParameter.MODULE,
        structlog.processors.CallsiteParameter.FUNC_NAME,
        structlog.processors.CallsiteParameter.LINENO,
    }
)


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    if not structlog.is_configured():
        install_fallback_pipeline()
    logger: structlog.stdlib.BoundLogger = structlog.stdlib.get_logger(name)
    return logger


def install_fallback_pipeline() -> None:
    """Before configure() or after shutdown(): warnings and above, redacted, JSON on stderr."""
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            uppercase_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True, key="timestamp"),
            add_context,
            add_correlation_ids,
            structure_exception,
            structlog.processors.EventRenamer("message"),
            RedactUntrustedFields(Redactor(SecurityConfig())),
            leading_fields_first,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING),
        logger_factory=structlog.PrintLoggerFactory(sys.stderr),
        cache_logger_on_first_use=False,
    )


class LoggingComponent:
    name = "logging"

    def __init__(self, config: ObservabilityConfig, redactor: Redactor) -> None:
        self._config = config
        self._redactor = redactor
        self._root = logging.getLogger()
        self._previous_root_level = self._root.level
        self._logger_provider: LoggerProvider | None = None

    def start(self) -> None:
        handlers = [self._output_handler()]
        if self._config.logging.export_to_otel:
            handlers.append(self._otel_handler())
        self._previous_root_level = self._root.level
        replace_our_handlers(self._root, handlers)
        self._root.setLevel(self._config.logging.level)
        structlog.configure(
            processors=[
                structlog.stdlib.filter_by_level,
                *self._enrichment(),
                structlog.stdlib.PositionalArgumentsFormatter(),
                *self._finalization(),
                structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
            ],
            logger_factory=structlog.stdlib.LoggerFactory(),
            wrapper_class=structlog.stdlib.BoundLogger,
            cache_logger_on_first_use=False,
        )

    def shutdown(self, timeout_s: float) -> None:
        remove_our_handlers(self._root)
        self._root.setLevel(self._previous_root_level)
        install_fallback_pipeline()
        if self._logger_provider is not None:
            self._logger_provider.force_flush(to_millis(timeout_s))
            self._logger_provider.shutdown()

    def _output_handler(self) -> logging.Handler:
        handler = build_handler(self._config.logging)
        handler.setFormatter(self._formatter(_stream_of(handler)))
        return mark_ours(handler, self._config.logging)

    def _otel_handler(self) -> logging.Handler:
        tracing = self._config.tracing
        self._logger_provider = LoggerProvider(resource=build_resource(self._config))
        self._logger_provider.add_log_record_processor(
            BatchLogRecordProcessor(
                FailureCountingLogExporter(log_exporters.create(tracing.exporter, config=tracing))
            )
        )
        handler = RedactedOtelLogHandler(self._logger_provider, self._redactor)
        return mark_ours(handler, self._config.logging)

    def _enrichment(self) -> list[Processor]:
        processors: list[Processor] = [
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            uppercase_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True, key="timestamp"),
            add_context,
            ServiceMetadata(self._config),
            add_correlation_ids,
        ]
        if self._config.logging.include_caller:
            processors.append(_CALLSITE)
        return processors

    def _finalization(self) -> list[Processor]:
        """Runs before a record reaches any handler, so foreign handlers never see raw values."""
        return [
            structure_exception,
            structlog.processors.EventRenamer("message"),
            RedactUntrustedFields(self._redactor),
            leading_fields_first,
        ]

    def _formatter(self, stream: TextIO | None) -> structlog.stdlib.ProcessorFormatter:
        return structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=[*self._enrichment(), *self._finalization()],
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                renderer_for(self._config.logging, stream),
            ],
        )


def _stream_of(handler: logging.Handler) -> TextIO | None:
    return handler.stream if isinstance(handler, logging.StreamHandler) else None
