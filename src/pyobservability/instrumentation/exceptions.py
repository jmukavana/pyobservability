import sys
import threading
from types import TracebackType

from pyobservability.logging import get_logger

UNHANDLED_LOGGER = "pyobservability.unhandled"


class UnhandledExceptionHooks:
    """Logs uncaught exceptions, then hands them to the hooks that were installed before."""

    name = "unhandled-exceptions"

    def __init__(self) -> None:
        self._previous_hook = sys.excepthook
        self._previous_thread_hook = threading.excepthook

    def start(self) -> None:
        self._previous_hook = sys.excepthook
        self._previous_thread_hook = threading.excepthook
        sys.excepthook = self._on_unhandled
        threading.excepthook = self._on_unhandled_in_thread

    def shutdown(self, timeout_s: float) -> None:
        if sys.excepthook == self._on_unhandled:
            sys.excepthook = self._previous_hook
        if threading.excepthook == self._on_unhandled_in_thread:
            threading.excepthook = self._previous_thread_hook

    def _on_unhandled(
        self,
        exc_type: type[BaseException],
        exc: BaseException,
        traceback: TracebackType | None,
    ) -> None:
        if isinstance(exc, Exception):
            get_logger(UNHANDLED_LOGGER).critical(
                "unhandled exception", exc_info=(exc_type, exc, traceback)
            )
        self._previous_hook(exc_type, exc, traceback)

    def _on_unhandled_in_thread(self, args: threading.ExceptHookArgs) -> None:
        if isinstance(args.exc_value, Exception):
            get_logger(UNHANDLED_LOGGER).critical(
                "unhandled exception in thread",
                thread=args.thread.name if args.thread else None,
                exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
            )
        self._previous_thread_hook(args)
