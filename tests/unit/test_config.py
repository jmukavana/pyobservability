from pathlib import Path
from typing import Any

import pytest

from pyobservability import ConfigurationError, SecurityPolicyError
from pyobservability.config import (
    CONFIG_FILE_ENV_VAR,
    SECURITY_WEAKENING_GRANT_ENV_VAR,
    SecurityConfig,
    load_config,
    parse_resource_attributes,
)


def write_config_file(path: Path, body: str) -> Path:
    path.write_text(body)
    return path


class TestSourcePrecedence:
    def test_defaults_apply_without_any_source(self, settings: dict[str, Any]) -> None:
        config = load_config(**settings)

        assert config.environment == "development"
        assert config.tracing.exporter == "console"

    def test_config_file_overrides_defaults(self, settings: dict[str, Any]) -> None:
        write_config_file(Path("pyobservability.toml"), "[logging]\nlevel = 'DEBUG'\n")

        assert load_config(**settings).logging.level == "DEBUG"

    def test_pyproject_table_is_read(self, settings: dict[str, Any]) -> None:
        write_config_file(
            Path("pyproject.toml"), "[tool.pyobservability.logging]\nlevel = 'ERROR'\n"
        )

        assert load_config(**settings).logging.level == "ERROR"

    @pytest.mark.parametrize(
        "body", ["[tool.ruff]\nline-length = 100\n", "[project]\nname = 'x'\n", ""]
    )
    def test_pyproject_without_our_table_is_ignored(
        self, settings: dict[str, Any], body: str
    ) -> None:
        write_config_file(Path("pyproject.toml"), body)

        assert load_config(**settings).logging.level == "INFO"

    def test_config_file_beats_pyproject(self, settings: dict[str, Any]) -> None:
        write_config_file(
            Path("pyproject.toml"), "[tool.pyobservability.logging]\nlevel = 'ERROR'\n"
        )
        write_config_file(Path("pyobservability.toml"), "[logging]\nlevel = 'DEBUG'\n")

        assert load_config(**settings).logging.level == "DEBUG"

    def test_explicit_config_file_path_from_environment(
        self, settings: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        path = write_config_file(tmp_path / "custom.toml", "service_version = '9.9.9'\n")
        monkeypatch.setenv(CONFIG_FILE_ENV_VAR, str(path))

        assert load_config(**settings).service_version == "9.9.9"

    def test_missing_explicit_config_file_is_an_error(
        self, settings: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(CONFIG_FILE_ENV_VAR, "/nonexistent/pyobservability.toml")

        with pytest.raises(ConfigurationError, match="missing file"):
            load_config(**settings)

    def test_environment_overrides_config_file(
        self, settings: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_config_file(Path("pyobservability.toml"), "[logging]\nlevel = 'DEBUG'\n")
        monkeypatch.setenv("PYOBS_LOGGING__LEVEL", "WARNING")

        assert load_config(**settings).logging.level == "WARNING"

    def test_python_arguments_override_environment(
        self, settings: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PYOBS_LOGGING__LEVEL", "WARNING")

        config = load_config(**settings, logging={"level": "CRITICAL"})

        assert config.logging.level == "CRITICAL"

    def test_nested_sections_merge_across_sources(
        self, settings: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PYOBS_TRACING__SAMPLE_RATE", "0.25")

        config = load_config(**settings, tracing={"protocol": "http"})

        assert (config.tracing.sample_rate, config.tracing.protocol) == (0.25, "http")


class TestOpenTelemetryEnvironment:
    def test_service_name_falls_back_to_otel_variable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OTEL_SERVICE_NAME", "otel-service")

        assert load_config(auditing={"enabled": False}).service_name == "otel-service"

    def test_pyobs_variable_beats_otel_variable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OTEL_SERVICE_NAME", "otel-service")
        monkeypatch.setenv("PYOBS_SERVICE_NAME", "pyobs-service")

        assert load_config(auditing={"enabled": False}).service_name == "pyobs-service"

    def test_signal_specific_endpoint_beats_generic_endpoint(
        self, settings: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4317")
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "http://traces:4317")

        config = load_config(**settings)

        assert config.tracing.endpoint == "http://traces:4317"
        assert config.metrics.endpoint == "http://collector:4317"

    def test_resource_attributes_are_parsed_and_unquoted(self) -> None:
        parsed = parse_resource_attributes("team=claims, region=eu%20west,malformed,=empty")

        assert parsed == {"team": "claims", "region": "eu west"}


class TestValidation:
    def test_service_name_is_required(self) -> None:
        with pytest.raises(ConfigurationError, match="service_name"):
            load_config()

    def test_unknown_keys_are_rejected(self, settings: dict[str, Any]) -> None:
        with pytest.raises(ConfigurationError, match="smaple_rate"):
            load_config(**settings, tracing={"smaple_rate": 0.5})

    @pytest.mark.parametrize("sample_rate", [-0.1, 1.1])
    def test_sample_rate_must_be_a_probability(
        self, settings: dict[str, Any], sample_rate: float
    ) -> None:
        with pytest.raises(ConfigurationError, match="sample_rate"):
            load_config(**settings, tracing={"sample_rate": sample_rate})

    @pytest.mark.parametrize("sample_rate", [0.0, 1.0])
    def test_sample_rate_accepts_bounds(self, settings: dict[str, Any], sample_rate: float) -> None:
        assert load_config(**settings, tracing={"sample_rate": sample_rate})

    def test_log_level_is_case_insensitive(self, settings: dict[str, Any]) -> None:
        assert load_config(**settings, logging={"level": "debug"}).logging.level == "DEBUG"

    def test_file_logging_needs_a_path(self, settings: dict[str, Any]) -> None:
        with pytest.raises(ConfigurationError, match="file_path"):
            load_config(**settings, logging={"output": "file"})

    def test_otlp_exporter_needs_an_endpoint(self, settings: dict[str, Any]) -> None:
        with pytest.raises(ConfigurationError, match="endpoint"):
            load_config(**settings, tracing={"exporter": "otlp"})

    def test_disabled_signal_does_not_need_an_endpoint(self, settings: dict[str, Any]) -> None:
        assert load_config(**settings, tracing={"exporter": "otlp", "enabled": False})

    def test_batch_size_cannot_exceed_queue(self, settings: dict[str, Any]) -> None:
        with pytest.raises(ConfigurationError, match="max_export_batch_size"):
            load_config(**settings, tracing={"max_queue_size": 10, "max_export_batch_size": 11})

    def test_disk_durability_needs_spool_dir(self) -> None:
        with pytest.raises(ConfigurationError, match="spool_dir"):
            load_config(service_name="svc", auditing={"durability": {"boundary": "disk"}})

    def test_hash_strategy_needs_salt(self, settings: dict[str, Any]) -> None:
        with pytest.raises(ConfigurationError, match="hash_salt"):
            load_config(**settings, security={"pii_strategy": "hash"})

    def test_invalid_redaction_pattern_is_rejected(self, settings: dict[str, Any]) -> None:
        with pytest.raises(ConfigurationError, match="invalid redaction pattern"):
            load_config(**settings, security={"extra_patterns": ["("]})

    def test_config_is_immutable(self, settings: dict[str, Any]) -> None:
        config = load_config(**settings)

        with pytest.raises(ValueError, match="frozen"):
            config.service_name = "changed"

    def test_secrets_are_hidden_from_repr(self, settings: dict[str, Any]) -> None:
        config = load_config(**settings, tracing={"headers": {"api-key": "s3cr3t-value"}})

        assert "s3cr3t-value" not in repr(config)
        assert "s3cr3t-value" not in str(config.model_dump())


class TestEnvironmentDefaults:
    def test_production_exports_over_otlp(self) -> None:
        config = load_config(
            service_name="svc",
            environment="production",
            tracing={"endpoint": "http://collector:4317"},
            metrics={"endpoint": "http://collector:4317"},
            auditing={"enabled": False},
        )

        assert (config.tracing.exporter, config.metrics.exporter) == ("otlp", "otlp")

    def test_explicit_exporter_wins_in_production(self) -> None:
        config = load_config(
            service_name="svc",
            environment="production",
            tracing={"exporter": "none"},
            metrics={"exporter": "console"},
            auditing={"enabled": False},
        )

        assert (config.tracing.exporter, config.metrics.exporter) == ("none", "console")

    def test_production_rejects_memory_only_audit(self) -> None:
        with pytest.raises(ConfigurationError, match="must not be held only in memory"):
            load_config(
                service_name="svc",
                environment="production",
                tracing={"exporter": "none"},
                metrics={"exporter": "none"},
            )

    def test_production_accepts_memory_audit_when_explicitly_allowed(self) -> None:
        config = load_config(
            service_name="svc",
            environment="production",
            tracing={"exporter": "none"},
            metrics={"exporter": "none"},
            auditing={"allow_memory_durability": True},
        )

        assert not config.auditing.is_durable


class TestSecurityLock:
    def test_secure_defaults_are_not_weakened(self) -> None:
        assert SecurityConfig().weakened_fields() == []

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("mask_secrets", False),
            ("redact_pii", False),
            ("path_param_redaction", False),
            ("capture_request_body", True),
            ("sql_parameters", True),
            ("capture_headers", frozenset({"authorization"})),
            ("pii_fields", frozenset({"email"})),
        ],
    )
    def test_weakening_in_code_requires_permission(self, field: str, value: Any) -> None:
        with pytest.raises(SecurityPolicyError, match=field):
            SecurityConfig(**{field: value})

    def test_weakening_in_code_with_permission_is_allowed(self, settings: dict[str, Any]) -> None:
        config = load_config(**settings, security={"mask_secrets": False, "allow_weakening": True})

        assert config.security.mask_secrets is False

    def test_strengthening_is_always_allowed(self, settings: dict[str, Any]) -> None:
        config = load_config(
            **settings,
            security={"pii_fields": ["email", "phone", "iban", *SecurityConfig().pii_fields]},
        )

        assert "iban" in config.security.pii_fields

    def test_environment_may_strengthen(
        self, settings: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PYOBS_SECURITY__EXTRA_SECRET_KEYS", '["pin_code"]')

        assert load_config(**settings).security.extra_secret_keys == {"pin_code"}

    def test_environment_may_not_weaken(
        self, settings: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PYOBS_SECURITY__MASK_SECRETS", "false")

        with pytest.raises(SecurityPolicyError, match="environment variables may not weaken"):
            load_config(**settings)

    def test_environment_may_weaken_with_explicit_grant(
        self, settings: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PYOBS_SECURITY__MASK_SECRETS", "false")
        monkeypatch.setenv(SECURITY_WEAKENING_GRANT_ENV_VAR, "true")

        assert load_config(**settings).security.mask_secrets is False

    def test_environment_cannot_set_code_only_permission(
        self, settings: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PYOBS_SECURITY__ALLOW_WEAKENING", "true")

        with pytest.raises(SecurityPolicyError, match="only be set in code"):
            load_config(**settings)

    def test_config_file_may_not_weaken_even_with_grant(
        self, settings: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_config_file(Path("pyobservability.toml"), "[security]\ncapture_request_body = true\n")
        monkeypatch.setenv(SECURITY_WEAKENING_GRANT_ENV_VAR, "true")

        with pytest.raises(SecurityPolicyError, match="config file may not weaken"):
            load_config(**settings)
