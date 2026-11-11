import importlib
import pkgutil

import pytest

import pyobservability


def _all_module_names() -> list[str]:
    walker = pkgutil.walk_packages(pyobservability.__path__, prefix="pyobservability.")
    return sorted(module.name for module in walker)


@pytest.mark.parametrize("module_name", _all_module_names())
def test_module_imports_without_optional_extras(module_name: str) -> None:
    importlib.import_module(module_name)
