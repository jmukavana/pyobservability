from collections.abc import Callable
from importlib.metadata import entry_points
from typing import Any

from pyobservability.exceptions import ConfigurationError


class Registry[T]:
    """Named factories, extended by third-party plugins through an entry-point group."""

    def __init__(self, kind: str, *, entry_point_group: str | None = None) -> None:
        self._kind = kind
        self._entry_point_group = entry_point_group
        self._factories: dict[str, Callable[..., T]] = {}
        self._plugins_loaded = False

    def register(self, name: str, factory: Callable[..., T]) -> None:
        if name in self._factories:
            raise ConfigurationError(f"{self._kind} {name!r} is already registered")
        self._factories[name] = factory

    def create(self, name: str, **options: Any) -> T:
        return self._factory(name)(**options)

    def names(self) -> list[str]:
        self._load_plugins()
        return sorted(self._factories)

    def _factory(self, name: str) -> Callable[..., T]:
        self._load_plugins()
        try:
            return self._factories[name]
        except KeyError:
            known = ", ".join(self.names()) or "none"
            raise ConfigurationError(
                f"unknown {self._kind} {name!r}; registered: {known}"
            ) from None

    def _load_plugins(self) -> None:
        if self._plugins_loaded or self._entry_point_group is None:
            return
        self._plugins_loaded = True
        for entry_point in entry_points(group=self._entry_point_group):
            try:
                factory = entry_point.load()
            except Exception as error:
                raise ConfigurationError(
                    f"{self._kind} plugin {entry_point.name!r} ({entry_point.value}) "
                    f"failed to load: {error}"
                ) from error
            self._factories.setdefault(entry_point.name, factory)
