from importlib.metadata import version

import pyobservability

PUBLIC_API = {
    "AlreadyConfiguredError",
    "AuditDurabilityError",
    "AuditValidationError",
    "ConfigurationError",
    "MissingExtraError",
    "NotConfiguredError",
    "ObservabilityConfig",
    "PyObservabilityError",
    "PyObservabilityWarning",
    "Runtime",
    "SecurityPolicyError",
    "ShutdownReport",
    "__version__",
    "ashutdown",
    "audit",
    "configure",
    "context",
    "get_logger",
    "get_meter",
    "get_tracer",
    "instrument",
    "shutdown",
    "traced",
    "uninstrument",
}


def test_public_api_matches_snapshot() -> None:
    assert set(pyobservability.__all__) == PUBLIC_API


def test_every_public_name_resolves() -> None:
    for name in pyobservability.__all__:
        assert hasattr(pyobservability, name), name


def test_version_comes_from_distribution_metadata() -> None:
    assert pyobservability.__version__ == version("pyobservability")


def test_all_errors_share_one_base_class() -> None:
    errors = [
        pyobservability.AlreadyConfiguredError,
        pyobservability.ConfigurationError,
        pyobservability.SecurityPolicyError,
    ]

    assert all(issubclass(error, pyobservability.PyObservabilityError) for error in errors)
