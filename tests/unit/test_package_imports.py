import importlib
import pkgutil

import pytest

import pyobservability
from pyobservability import MissingExtraError

OPTIONAL_EXTRA_MODULES = (
    "pyobservability.integrations.",
    "pyobservability.audit.exporters.kafka",
    "pyobservability.audit.exporters.postgres",
    "pyobservability.audit.outbox",
)


def _all_module_names() -> list[str]:
    walker = pkgutil.walk_packages(pyobservability.__path__, prefix="pyobservability.")
    return sorted(module.name for module in walker)


@pytest.mark.parametrize("module_name", _all_module_names())
def test_module_imports_or_names_its_missing_extra(module_name: str) -> None:
    """Core modules import without extras; optional-extra modules may only ask for theirs."""
    try:
        importlib.import_module(module_name)
    except MissingExtraError:
        if not module_name.startswith(OPTIONAL_EXTRA_MODULES):
            raise
