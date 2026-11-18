import functools
import inspect
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, cast, overload

from opentelemetry import trace
from opentelemetry.semconv.attributes.code_attributes import CODE_FUNCTION_NAME
from opentelemetry.util.types import AttributeValue


@overload
def traced[**P, R](function: Callable[P, R], /) -> Callable[P, R]: ...


@overload
def traced[**P, R](
    *,
    name: str | None = None,
    attributes: Mapping[str, AttributeValue] | None = None,
    record_exceptions: bool = True,
) -> Callable[[Callable[P, R]], Callable[P, R]]: ...


def traced(
    function: Callable[..., Any] | None = None,
    /,
    *,
    name: str | None = None,
    attributes: Mapping[str, AttributeValue] | None = None,
    record_exceptions: bool = True,
) -> Any:
    """Wraps a sync or async function in a span; usable as `@traced` or `@traced(...)`."""

    def decorate(target: Callable[..., Any]) -> Callable[..., Any]:
        span = _SpanSettings(target, name, attributes, record_exceptions)
        if inspect.iscoroutinefunction(target):
            return _async_wrapper(cast(Callable[..., Awaitable[Any]], target), span)
        return _sync_wrapper(target, span)

    return decorate if function is None else decorate(function)


class _SpanSettings:
    def __init__(
        self,
        target: Callable[..., Any],
        name: str | None,
        attributes: Mapping[str, AttributeValue] | None,
        record_exceptions: bool,
    ) -> None:
        qualified_name = f"{target.__module__}.{target.__qualname__}"
        self.tracer = trace.get_tracer(target.__module__)
        self.name = name or target.__qualname__
        self.attributes = {CODE_FUNCTION_NAME: qualified_name, **(attributes or {})}
        self.record_exceptions = record_exceptions

    def start(self) -> Any:
        return self.tracer.start_as_current_span(
            self.name,
            attributes=self.attributes,
            record_exception=self.record_exceptions,
            set_status_on_exception=self.record_exceptions,
        )


def _sync_wrapper(target: Callable[..., Any], span: _SpanSettings) -> Callable[..., Any]:
    @functools.wraps(target)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        with span.start():
            return target(*args, **kwargs)

    return wrapper


def _async_wrapper(
    target: Callable[..., Awaitable[Any]], span: _SpanSettings
) -> Callable[..., Awaitable[Any]]:
    @functools.wraps(target)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        with span.start():
            return await target(*args, **kwargs)

    return wrapper
