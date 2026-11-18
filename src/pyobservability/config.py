import os
import re
import tomllib
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Annotated, Any, Literal, Self
from urllib.parse import unquote

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    SecretStr,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic.fields import FieldInfo
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

from pyobservability.exceptions import ConfigurationError, SecurityPolicyError

DEVELOPMENT = "development"
PRODUCTION = "production"

ENV_PREFIX = "PYOBS_"
CONFIG_FILE_ENV_VAR = "PYOBS_CONFIG_FILE"
SECURITY_WEAKENING_GRANT_ENV_VAR = "PYOBS_SECURITY__ALLOW_WEAKENING_FROM_ENV"
DEFAULT_CONFIG_FILE = Path("pyobservability.toml")
PYPROJECT_FILE = Path("pyproject.toml")
PYPROJECT_TABLE = ("tool", "pyobservability")

DEFAULT_PII_FIELDS = frozenset(
    {
        "email",
        "phone",
        "address",
        "dob",
        "date_of_birth",
        "first_name",
        "last_name",
        "full_name",
        "national_id",
        "passport_number",
    }
)

OTLP_EXPORTER = "otlp"
CONSOLE_EXPORTER = "console"
NO_EXPORTER = "none"
ProviderMode = Literal["auto", "own", "attach", "external"]
ResolvedProviderMode = Literal["own", "attach", "external"]
LogLevel = Annotated[
    Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
    BeforeValidator(lambda level: level.upper() if isinstance(level, str) else level),
]


class _Section(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class LoggingConfig(_Section):
    enabled: bool = True
    level: LogLevel = "INFO"
    format: Literal["json", "console"] = "json"
    output: Literal["stdout", "stderr", "file"] = "stdout"
    file_path: Path | None = None
    capture_stdlib: bool = True
    export_to_otel: bool = False
    include_caller: bool = False
    capture_unhandled_exceptions: bool = False

    @model_validator(mode="after")
    def _file_output_needs_path(self) -> Self:
        if self.output == "file" and self.file_path is None:
            raise ValueError("file_path is required when output='file'")
        return self


class TelemetryExportConfig(_Section):
    enabled: bool = True
    exporter: str = Field(default=CONSOLE_EXPORTER, min_length=1)
    endpoint: str | None = None
    protocol: Literal["grpc", "http"] = "grpc"
    headers: dict[str, SecretStr] = {}
    insecure: bool | None = None
    timeout_s: float = Field(default=10.0, gt=0)

    @property
    def exports(self) -> bool:
        return self.enabled and self.exporter != NO_EXPORTER

    @model_validator(mode="after")
    def _otlp_needs_endpoint(self) -> Self:
        if self.enabled and self.exporter == OTLP_EXPORTER and self.endpoint is None:
            raise ValueError(
                "endpoint is required when exporter='otlp' (or set OTEL_EXPORTER_OTLP_ENDPOINT)"
            )
        return self


class TracingConfig(TelemetryExportConfig):
    sample_rate: float = Field(default=1.0, ge=0.0, le=1.0)
    sampler: Literal["parent_based_ratio", "always_on", "always_off"] = "parent_based_ratio"
    max_queue_size: int = Field(default=2048, gt=0)
    max_export_batch_size: int = Field(default=512, gt=0)
    schedule_delay_ms: int = Field(default=5000, gt=0)
    export_timeout_ms: int = Field(default=30000, gt=0)
    span_attribute_limit: int = Field(default=128, gt=0)

    @model_validator(mode="after")
    def _batch_fits_in_queue(self) -> Self:
        if self.max_export_batch_size > self.max_queue_size:
            raise ValueError("max_export_batch_size cannot exceed max_queue_size")
        return self


class MetricsConfig(TelemetryExportConfig):
    export_interval_ms: int = Field(default=60000, gt=0)
    allowed_label_keys: frozenset[str] | None = None
    forbidden_label_keys: frozenset[str] = frozenset()
    on_forbidden_label: Literal["strip", "reject"] = "strip"
    max_cardinality_per_metric: int = Field(default=2000, gt=0)


class RetryConfig(_Section):
    max_attempts: int = Field(default=8, ge=1)
    initial_backoff_s: float = Field(default=0.5, gt=0)
    max_backoff_s: float = Field(default=60.0, gt=0)
    multiplier: float = Field(default=2.0, ge=1)
    jitter: float = Field(default=0.2, ge=0, le=1)
    dead_letter: Literal["log", "file"] = "log"

    @model_validator(mode="after")
    def _initial_backoff_within_max(self) -> Self:
        if self.initial_backoff_s > self.max_backoff_s:
            raise ValueError("initial_backoff_s cannot exceed max_backoff_s")
        return self


class OutboxConfig(_Section):
    """Where the transactional outbox lives; the relay delivers from it in the background."""

    dsn: SecretStr | None = None
    dsn_env: str | None = None
    schema_name: str = Field(default="public", pattern=r"^[a-z_][a-z0-9_]{0,62}$")
    create_schema: bool = True
    poll_interval_s: float = Field(default=1.0, gt=0)
    lease_s: float = Field(default=60.0, gt=0)

    @model_validator(mode="after")
    def _needs_a_connection(self) -> Self:
        if self.dsn is None and self.dsn_env is None:
            raise ValueError("the outbox needs dsn or dsn_env")
        return self


class DurabilityConfig(_Section):
    boundary: Literal["memory", "disk", "exporter", "outbox"] = "memory"
    spool_dir: Path | None = None
    fsync: bool = True
    outbox: OutboxConfig | None = None

    @model_validator(mode="after")
    def _disk_needs_spool_dir(self) -> Self:
        if self.boundary == "disk" and self.spool_dir is None:
            raise ValueError("spool_dir is required when boundary='disk'")
        return self

    @model_validator(mode="after")
    def _outbox_boundary_needs_outbox(self) -> Self:
        if self.boundary == "outbox" and self.outbox is None:
            raise ValueError("durability.outbox is required when boundary='outbox'")
        return self


class AuditExporterConfig(_Section):
    name: str = Field(min_length=1)
    required: bool = True
    options: dict[str, Any] = Field(default={}, repr=False)


class IntegrityConfig(_Section):
    """HMAC keys by key ID; new records use the active key, older ones verify with theirs."""

    keys: dict[str, SecretStr] = {}
    active_key_id: str | None = None

    @model_validator(mode="after")
    def _active_key_is_configured(self) -> Self:
        if self.active_key_id is not None and self.active_key_id not in self.keys:
            raise ValueError(f"integrity.active_key_id {self.active_key_id!r} is not in keys")
        return self


class AuditConfig(_Section):
    enabled: bool = True
    delivery: Literal["sync", "async"] = "async"
    exporters: tuple[AuditExporterConfig, ...] = Field(
        default=(AuditExporterConfig(name="console"),), min_length=1
    )
    batch_size: int = Field(default=100, gt=0)
    queue_max_size: int = Field(default=10000, gt=0)
    on_queue_full: Literal["block", "raise"] = "block"
    block_timeout_s: float = Field(default=5.0, gt=0)
    retry: RetryConfig = RetryConfig()
    durability: DurabilityConfig = DurabilityConfig()
    integrity: IntegrityConfig = IntegrityConfig()
    tenant_required: bool = False
    allow_memory_durability: bool = False
    on_delivery_failure: Callable[[Any, str], None] | None = None

    @property
    def is_durable(self) -> bool:
        return self.durability.boundary != "memory"

    @property
    def delivers_inline(self) -> bool:
        return self.delivery == "sync"

    @model_validator(mode="after")
    def _inline_delivery_matches_exporter_boundary(self) -> Self:
        if self.delivers_inline != (self.durability.boundary == "exporter"):
            raise ValueError(
                "delivery='sync' and durability.boundary='exporter' must be used together: "
                "inline delivery is what makes the exporter the durability boundary"
            )
        return self

    @model_validator(mode="after")
    def _file_dead_letters_need_spool_dir(self) -> Self:
        if self.retry.dead_letter == "file" and self.durability.spool_dir is None:
            raise ValueError("retry.dead_letter='file' requires durability.spool_dir")
        return self


def _differs(value: Any, secure: Any) -> bool:
    return bool(value != secure)


def _captures_more(value: frozenset[str], secure: frozenset[str]) -> bool:
    return not value <= secure


def _protects_less(value: frozenset[str], secure: frozenset[str]) -> bool:
    return not value >= secure


_WEAKENING_RULES: Mapping[str, Callable[[Any, Any], bool]] = {
    "redact_pii": _differs,
    "mask_secrets": _differs,
    "path_param_redaction": _differs,
    "capture_request_body": _differs,
    "sql_parameters": _differs,
    "capture_headers": _captures_more,
    "pii_fields": _protects_less,
}


class SecurityConfig(_Section):
    redact_pii: bool = True
    mask_secrets: bool = True
    mask_token: str = Field(default="[REDACTED]", min_length=1)
    extra_secret_keys: frozenset[str] = frozenset()
    extra_patterns: tuple[str, ...] = ()
    pii_fields: frozenset[str] = DEFAULT_PII_FIELDS
    pii_strategy: Literal["mask", "hash", "drop"] = "mask"
    hash_salt: SecretStr | None = None
    capture_request_body: bool = False
    capture_headers: frozenset[str] = frozenset()
    path_param_redaction: bool = True
    sql_parameters: bool = False
    allow_weakening: bool = False

    @field_validator("extra_patterns")
    @classmethod
    def _patterns_compile(cls, patterns: tuple[str, ...]) -> tuple[str, ...]:
        for pattern in patterns:
            try:
                re.compile(pattern)
            except re.error as error:
                raise ValueError(f"invalid redaction pattern {pattern!r}: {error}") from error
        return patterns

    @model_validator(mode="after")
    def _hash_strategy_needs_salt(self) -> Self:
        if self.pii_strategy == "hash" and self.hash_salt is None:
            raise ValueError("hash_salt is required when pii_strategy='hash'")
        return self

    @model_validator(mode="after")
    def _weakening_requires_permission(self) -> Self:
        weakened = self.weakened_fields()
        if weakened and not self.allow_weakening:
            raise SecurityPolicyError(
                f"security settings {weakened} weaken the secure defaults; "
                "set security.allow_weakening=True in code to permit this"
            )
        return self

    def weakened_fields(self) -> list[str]:
        secure_defaults = type(self).model_fields
        return sorted(
            name
            for name, weakens in _WEAKENING_RULES.items()
            if weakens(getattr(self, name), secure_defaults[name].default)
        )


class ContextConfig(_Section):
    request_id_header: str = "X-Request-ID"
    trust_incoming_request_id: bool = True
    tenant_header: str | None = None
    max_value_length: int = Field(default=256, gt=0)


DEFAULT_EXCLUDED_PATHS = frozenset({"/health", "/healthz", "/livez", "/readyz", "/metrics"})


class HttpConfig(_Section):
    access_log: bool = True
    excluded_paths: frozenset[str] = DEFAULT_EXCLUDED_PATHS
    echo_request_id: bool = True


class _SecurityGuardedSource(PydanticBaseSettingsSource):
    """Stops non-code sources from weakening security; only an env grant may permit it."""

    def __init__(
        self, source: PydanticBaseSettingsSource, *, origin: str, honours_env_grant: bool
    ) -> None:
        super().__init__(source.settings_cls)
        self._source = source
        self._origin = origin
        self._honours_env_grant = honours_env_grant

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        return self._source.get_field_value(field, field_name)

    def __call__(self) -> dict[str, Any]:
        values = self._source()
        if not values.get("security"):
            return values
        security = self._without_permission_keys(values["security"])
        weakened = SecurityConfig(**security, allow_weakening=True).weakened_fields()
        if weakened and not self._weakening_granted():
            raise SecurityPolicyError(
                f"{self._origin} may not weaken security settings {weakened}; "
                f"set {SECURITY_WEAKENING_GRANT_ENV_VAR}=true to permit this deliberately"
            )
        return {**values, "security": {**security, "allow_weakening": bool(weakened)}}

    def _without_permission_keys(self, security: Mapping[str, Any]) -> dict[str, Any]:
        security = dict(security)
        if self._honours_env_grant:
            security.pop("allow_weakening_from_env", None)
        if "allow_weakening" in security:
            raise SecurityPolicyError(
                f"security.allow_weakening can only be set in code, not from {self._origin}"
            )
        return security

    def _weakening_granted(self) -> bool:
        grant = os.environ.get(SECURITY_WEAKENING_GRANT_ENV_VAR, "false")
        return self._honours_env_grant and TypeAdapter(bool).validate_python(grant)


_OTEL_ENDPOINT_VARIABLES = {
    "tracing": ("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "OTEL_EXPORTER_OTLP_ENDPOINT"),
    "metrics": ("OTEL_EXPORTER_OTLP_METRICS_ENDPOINT", "OTEL_EXPORTER_OTLP_ENDPOINT"),
}


class _OpenTelemetryEnvironmentSource(PydanticBaseSettingsSource):
    """Standard OTEL_* variables, ranked below PYOBS_* variables."""

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        values: dict[str, Any] = {}
        if service_name := os.environ.get("OTEL_SERVICE_NAME"):
            values["service_name"] = service_name
        if attributes := os.environ.get("OTEL_RESOURCE_ATTRIBUTES"):
            values["resource_attributes"] = parse_resource_attributes(attributes)
        for section, variables in _OTEL_ENDPOINT_VARIABLES.items():
            if endpoint := _first_set(variables):
                values[section] = {"endpoint": endpoint}
        return values


def _first_set(variables: tuple[str, ...]) -> str | None:
    return next((os.environ[name] for name in variables if os.environ.get(name)), None)


def parse_resource_attributes(raw: str) -> dict[str, str]:
    pairs = (item.split("=", 1) for item in raw.split(",") if "=" in item)
    return {unquote(key.strip()): unquote(value.strip()) for key, value in pairs if key.strip()}


def _config_file() -> Path:
    explicit = os.environ.get(CONFIG_FILE_ENV_VAR)
    if explicit is None:
        return DEFAULT_CONFIG_FILE
    path = Path(explicit)
    if not path.is_file():
        raise ConfigurationError(f"{CONFIG_FILE_ENV_VAR} points to a missing file: {path}")
    return path


def _with_production_exporter(section: Any) -> dict[str, Any]:
    if isinstance(section, BaseModel):
        section = section.model_dump(exclude_unset=True)
    return {"exporter": OTLP_EXPORTER, **(section or {})}


class ObservabilityConfig(BaseSettings):
    model_config = SettingsConfigDict(
        frozen=True,
        extra="forbid",
        env_prefix=ENV_PREFIX,
        env_nested_delimiter="__",
    )

    service_name: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._-]+$")
    service_version: str = "0.0.0"
    environment: str = Field(default=DEVELOPMENT, min_length=1)
    provider_mode: ProviderMode = "auto"
    resource_attributes: dict[str, str] = {}
    logging: LoggingConfig = LoggingConfig()
    tracing: TracingConfig = TracingConfig()
    metrics: MetricsConfig = MetricsConfig()
    auditing: AuditConfig = AuditConfig()
    security: SecurityConfig = SecurityConfig()
    context: ContextConfig = ContextConfig()
    http: HttpConfig = HttpConfig()
    shutdown_timeout_s: float = Field(default=10.0, gt=0)

    @property
    def is_production(self) -> bool:
        return self.environment == PRODUCTION

    @model_validator(mode="before")
    @classmethod
    def _export_over_otlp_outside_development(cls, data: Any) -> Any:
        if not isinstance(data, dict) or data.get("environment", DEVELOPMENT) == DEVELOPMENT:
            return data
        return {
            **data,
            "tracing": _with_production_exporter(data.get("tracing")),
            "metrics": _with_production_exporter(data.get("metrics")),
        }

    @model_validator(mode="after")
    def _production_audit_must_be_durable(self) -> Self:
        audit = self.auditing
        if (
            self.is_production
            and audit.enabled
            and not audit.is_durable
            and not audit.allow_memory_durability
        ):
            raise ValueError(
                "in production, audit events must not be held only in memory: set "
                "auditing.durability.boundary to 'disk' or 'exporter', or set "
                "auditing.allow_memory_durability=True to accept possible loss"
            )
        return self

    @model_validator(mode="after")
    def _log_export_needs_an_exporter(self) -> Self:
        if self.logging.export_to_otel and not self.tracing.exports:
            raise ValueError(
                "logging.export_to_otel sends logs through the tracing exporter settings, "
                "so tracing must be enabled with an exporter other than 'none'"
            )
        return self

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        config_file = TomlConfigSettingsSource(settings_cls, toml_file=_config_file())
        sources: tuple[PydanticBaseSettingsSource, ...] = (
            init_settings,
            _SecurityGuardedSource(
                env_settings, origin="environment variables", honours_env_grant=True
            ),
            _OpenTelemetryEnvironmentSource(settings_cls),
            _SecurityGuardedSource(config_file, origin="config file", honours_env_grant=False),
        )
        if not _pyproject_has_our_table():
            return sources
        pyproject = TomlConfigSettingsSource(
            settings_cls, toml_file=PYPROJECT_FILE, toml_table_header=PYPROJECT_TABLE
        )
        return (
            *sources,
            _SecurityGuardedSource(pyproject, origin="pyproject.toml", honours_env_grant=False),
        )


def _pyproject_has_our_table() -> bool:
    """pydantic-settings raises KeyError when the table is absent, as it is in most projects."""
    if not PYPROJECT_FILE.is_file():
        return False
    table: Any = tomllib.loads(PYPROJECT_FILE.read_text(encoding="utf-8"))
    for key in PYPROJECT_TABLE:
        if not isinstance(table, dict) or key not in table:
            return False
        table = table[key]
    return True


def load_config(**settings: Any) -> ObservabilityConfig:
    try:
        return ObservabilityConfig(**settings)
    except ValidationError as error:
        raise ConfigurationError(f"invalid pyobservability configuration:\n{error}") from error
