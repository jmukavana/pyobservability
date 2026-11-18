from importlib.metadata import EntryPoint

import pytest

from pyobservability import ConfigurationError
from pyobservability.core import registry as registry_module
from pyobservability.core.registry import Registry

PLUGIN_GROUP = "pyobservability.test_plugins"


def use_entry_points(monkeypatch: pytest.MonkeyPatch, *entry_points: EntryPoint) -> None:
    def fake_entry_points(*, group: str) -> tuple[EntryPoint, ...]:
        return entry_points if group == PLUGIN_GROUP else ()

    monkeypatch.setattr(registry_module, "entry_points", fake_entry_points)


def test_creates_registered_factory_with_options() -> None:
    registry: Registry[dict[str, int]] = Registry("exporter")
    registry.register("dict", dict)

    assert registry.create("dict", size=3) == {"size": 3}


def test_duplicate_registration_is_rejected() -> None:
    registry: Registry[object] = Registry("exporter")
    registry.register("console", object)

    with pytest.raises(ConfigurationError, match="already registered"):
        registry.register("console", object)


def test_unknown_name_lists_registered_names() -> None:
    registry: Registry[object] = Registry("exporter")
    registry.register("console", object)

    with pytest.raises(ConfigurationError, match="unknown exporter 'kafka'; registered: console"):
        registry.create("kafka")


def test_plugins_are_discovered_from_entry_points(monkeypatch: pytest.MonkeyPatch) -> None:
    use_entry_points(monkeypatch, EntryPoint("ordered", "collections:OrderedDict", PLUGIN_GROUP))
    registry: Registry[object] = Registry("exporter", entry_point_group=PLUGIN_GROUP)

    assert registry.names() == ["ordered"]


def test_builtin_registration_wins_over_plugin(monkeypatch: pytest.MonkeyPatch) -> None:
    use_entry_points(monkeypatch, EntryPoint("console", "collections:OrderedDict", PLUGIN_GROUP))
    registry: Registry[object] = Registry("exporter", entry_point_group=PLUGIN_GROUP)
    registry.register("console", list)

    assert registry.create("console") == []


def test_broken_plugin_raises_configuration_error(monkeypatch: pytest.MonkeyPatch) -> None:
    use_entry_points(monkeypatch, EntryPoint("broken", "no_such_module:Factory", PLUGIN_GROUP))
    registry: Registry[object] = Registry("exporter", entry_point_group=PLUGIN_GROUP)

    with pytest.raises(ConfigurationError, match="plugin 'broken'"):
        registry.names()
