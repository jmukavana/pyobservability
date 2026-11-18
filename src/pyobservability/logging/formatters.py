from typing import TextIO

import structlog
from structlog.typing import Processor

from pyobservability.config import LoggingConfig


def renderer_for(config: LoggingConfig, stream: TextIO | None) -> Processor:
    if config.format == "console":
        return structlog.dev.ConsoleRenderer(colors=bool(stream and stream.isatty()))
    return structlog.processors.JSONRenderer()
