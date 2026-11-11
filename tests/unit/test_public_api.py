from importlib.metadata import version

import pyobservability

PUBLIC_API = {"__version__"}


def test_public_api_matches_snapshot() -> None:
    assert set(pyobservability.__all__) == PUBLIC_API


def test_every_public_name_resolves() -> None:
    for name in pyobservability.__all__:
        assert hasattr(pyobservability, name), name


def test_version_comes_from_distribution_metadata() -> None:
    assert pyobservability.__version__ == version("pyobservability")
