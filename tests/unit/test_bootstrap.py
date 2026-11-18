import warnings
from typing import Any

import pytest
from opentelemetry.sdk.trace import TracerProvider as SdkTracerProvider
from opentelemetry.trace import NoOpTracerProvider, ProxyTracerProvider

import pyobservability
from pyobservability import (
    AlreadyConfiguredError,
    ConfigurationError,
    ObservabilityConfig,
    PyObservabilityWarning,
)
from pyobservability.runtime import active_runtime
from pyobservability.tracing import resolve_provider_mode


def test_configure_returns_active_runtime(settings: dict[str, Any]) -> None:
    runtime = pyobservability.configure(**settings)

    assert runtime.config.service_name == "test-service"
    assert active_runtime.current is runtime


def test_configure_accepts_a_config_object(settings: dict[str, Any]) -> None:
    config = ObservabilityConfig(**settings)

    assert pyobservability.configure(config).config is config


def test_config_object_and_settings_together_are_rejected(settings: dict[str, Any]) -> None:
    with pytest.raises(ConfigurationError, match="not both"):
        pyobservability.configure(ObservabilityConfig(**settings), service_name="other")


def test_repeated_configure_with_same_settings_is_a_no_op(settings: dict[str, Any]) -> None:
    first = pyobservability.configure(**settings)

    assert pyobservability.configure(**settings) is first


def test_configure_with_different_settings_is_rejected(settings: dict[str, Any]) -> None:
    pyobservability.configure(**settings)

    with pytest.raises(AlreadyConfiguredError):
        pyobservability.configure(**{**settings, "service_name": "other"})


def test_reconfigure_replaces_the_runtime(settings: dict[str, Any]) -> None:
    first = pyobservability.configure(**settings)

    second = pyobservability.configure(**{**settings, "service_name": "other"}, reconfigure=True)

    assert second is not first
    assert active_runtime.current is second


def test_invalid_settings_raise_configuration_error() -> None:
    with pytest.raises(ConfigurationError):
        pyobservability.configure(service_name="")


def test_shutdown_clears_the_runtime(settings: dict[str, Any]) -> None:
    pyobservability.configure(**settings)

    report = pyobservability.shutdown()

    assert report.succeeded
    assert active_runtime.current is None


def test_shutdown_without_configure_is_harmless() -> None:
    assert pyobservability.shutdown().components == ()


async def test_async_shutdown(settings: dict[str, Any]) -> None:
    pyobservability.configure(**settings)

    report = await pyobservability.ashutdown()

    assert report.succeeded
    assert active_runtime.current is None


def test_memory_only_audit_warns_about_possible_loss() -> None:
    with pytest.warns(PyObservabilityWarning, match="lost if the process crashes"):
        pyobservability.configure(service_name="svc")


def test_durable_audit_does_not_warn() -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        pyobservability.configure(
            service_name="svc",
            auditing={"delivery": "sync", "durability": {"boundary": "exporter"}},
        )


class TestProviderMode:
    def test_auto_owns_providers_when_none_installed(self) -> None:
        assert resolve_provider_mode("auto", ProxyTracerProvider()) == "own"

    def test_auto_attaches_to_existing_sdk_provider(self) -> None:
        assert resolve_provider_mode("auto", SdkTracerProvider()) == "attach"

    def test_auto_stays_external_for_foreign_provider(self) -> None:
        assert resolve_provider_mode("auto", NoOpTracerProvider()) == "external"

    def test_explicit_mode_is_respected(self) -> None:
        assert resolve_provider_mode("external", ProxyTracerProvider()) == "external"

    def test_own_conflicts_with_installed_provider(self) -> None:
        with pytest.raises(ConfigurationError, match="already installed"):
            resolve_provider_mode("own", SdkTracerProvider())
