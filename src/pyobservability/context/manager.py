import re
from collections.abc import Mapping
from contextvars import ContextVar, Token
from types import MappingProxyType, TracebackType
from typing import Self

ContextValue = str | int | float | bool

DEFAULT_MAX_VALUE_LENGTH = 256
_KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]{0,63}$")
_EMPTY: Mapping[str, ContextValue] = MappingProxyType({})

_attributes: ContextVar[Mapping[str, ContextValue]] = ContextVar(
    "pyobservability_context", default=_EMPTY
)


class _Limits:
    max_value_length = DEFAULT_MAX_VALUE_LENGTH


limits = _Limits()


class Scope:
    """Adds attributes for the duration of a `with` / `async with` block, then restores."""

    def __init__(self, attributes: Mapping[str, ContextValue]) -> None:
        self._attributes = attributes
        self._token: Token[Mapping[str, ContextValue]] | None = None

    def __enter__(self) -> Self:
        self._token = _attributes.set(_merged_with_current(self._attributes))
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._token is not None:
            _attributes.reset(self._token)
            self._token = None

    async def __aenter__(self) -> Self:
        return self.__enter__()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.__exit__(exc_type, exc, traceback)


def scope(**attributes: ContextValue) -> Scope:
    return Scope(attributes)


def set(**attributes: ContextValue) -> None:
    _attributes.set(_merged_with_current(attributes))


def get(key: str) -> ContextValue | None:
    return _attributes.get().get(key)


def snapshot() -> dict[str, ContextValue]:
    return dict(_attributes.get())


def clear() -> None:
    _attributes.set(_EMPTY)


def _merged_with_current(attributes: Mapping[str, ContextValue]) -> Mapping[str, ContextValue]:
    validated = {key: _validated_value(key, value) for key, value in attributes.items()}
    return MappingProxyType({**_attributes.get(), **validated})


def _validated_value(key: str, value: ContextValue) -> ContextValue:
    if not _KEY_PATTERN.fullmatch(key):
        raise ValueError(f"invalid context key {key!r}")
    if not isinstance(value, ContextValue):
        raise TypeError(
            f"context value for {key!r} must be str, int, float or bool, got {type(value).__name__}"
        )
    if isinstance(value, str):
        return value[: limits.max_value_length]
    return value
