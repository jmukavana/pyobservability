# Configuration

`configure(**settings)` validates everything up front and raises `ConfigurationError` on mistakes,
including unknown keys. It is idempotent: the same settings return the running instance, different
settings raise `AlreadyConfiguredError` unless you pass `reconfigure=True`.

## Where settings come from

Highest priority first:

1. Arguments to `configure()`.
2. `PYOBS_*` environment variables. Nest with `__`: `PYOBS_TRACING__SAMPLE_RATE=0.1`.
3. Standard `OTEL_SERVICE_NAME`, `OTEL_RESOURCE_ATTRIBUTES`, `OTEL_EXPORTER_OTLP_[TRACES|METRICS_]ENDPOINT`.
4. `pyobservability.toml` (or the file named by `PYOBS_CONFIG_FILE`).
5. `[tool.pyobservability]` in `pyproject.toml`.

Nested sections merge across sources.

## Top level

| Key | Default | Notes |
|---|---|---|
| `service_name` | required | `[A-Za-z0-9._-]+` |
| `service_version` | `0.0.0` | |
| `environment` | `development` | outside `development`, tracing and metrics default to the `otlp` exporter |
| `provider_mode` | `auto` | `own`, `attach` (add a processor to an existing SDK tracer provider) or `external` (leave OpenTelemetry alone) |
| `shutdown_timeout_s` | `10` | shared across components |

## Sections

| Section | Keys |
|---|---|
| `logging` | `enabled`, `level`, `format` (`json`/`console`), `output` (`stdout`/`stderr`/`file`), `file_path`, `capture_stdlib`, `export_to_otel`, `include_caller`, `capture_unhandled_exceptions` |
| `tracing` | `enabled`, `exporter` (`console`/`otlp`/`none`/plugin), `endpoint`, `protocol` (`grpc`/`http`), `headers`, `insecure`, `timeout_s`, `sample_rate`, `sampler`, `max_queue_size`, `max_export_batch_size`, `schedule_delay_ms`, `export_timeout_ms`, `span_attribute_limit` |
| `metrics` | `enabled`, `exporter`, `endpoint`, `protocol`, `headers`, `insecure`, `export_interval_ms`, `allowed_label_keys`, `forbidden_label_keys`, `on_forbidden_label`, `max_cardinality_per_metric` |
| `auditing` | see [Auditing](auditing.md) |
| `security` | see [Security](security.md) |
| `context` | `request_id_header`, `trust_incoming_request_id`, `tenant_header`, `max_value_length` |
| `http` | `access_log`, `excluded_paths`, `echo_request_id` |

Header values and DSNs are `SecretStr`: they never appear in `repr()` or logs.

`insecure` is left unset by default so the endpoint scheme decides: `http://…` is plaintext and `https://…` always uses TLS, even if `insecure=True` is set.

## Rules that fail fast

- `exporter="otlp"` needs an `endpoint` (or an `OTEL_EXPORTER_OTLP_*ENDPOINT`).
- `logging.export_to_otel` reuses the tracing exporter settings, so tracing must export.
- In `production`, keeping audit events only in memory is an error unless
  `auditing.allow_memory_durability=True`.
- Environment variables and files cannot weaken [security settings](security.md#the-security-lock).
