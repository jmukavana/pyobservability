# Changelog

All notable changes follow [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Fixed
- Request IDs, context keys and PostgreSQL schema names ending in a newline were accepted by their validation patterns (`$` matches before a trailing newline); they now use a full match.
- Redaction no longer slows down on long unbroken strings: four patterns (URL credentials, secret query values, emails and card numbers) backtracked quadratically, so a 16 KB base64 blob or hostile input took 250-780 ms to redact. All rules are now length-bounded and the worst case is ~30 ms, scaling linearly.
- Masking many card numbers in one string no longer recurses once per card.
- OTLP gRPC exporters no longer force TLS on `http://` endpoints: `insecure` is now inferred from the endpoint scheme unless set explicitly (`https://` always uses TLS).
- Recording an audit event in a process forked after `configure()` now raises `AuditDurabilityError` instead of queueing events no thread will deliver.
- `configure()` no longer crashes when the working directory has a `pyproject.toml` without a `[tool.pyobservability]` table.

### Changed
- Raised minimum versions so the lowest-direct CI job can install and run on Python 3.12 to 3.14: `psycopg>=3.2.10`, `aiokafka>=0.13.0` and `sqlalchemy>=2.0.45`.

### Added
- Audit engine: `audit.record()` / `arecord()` / `flush()` with memory, disk (write-ahead log), exporter and outbox durability boundaries, retries with backoff, dead letters and idempotent delivery on `event_id`; console, file, PostgreSQL and Kafka exporters.
- Tamper evidence: per-tenant HMAC hash chains computed by PostgreSQL, append-only triggers and `audit.verify()`.
- Transactional outbox: `audit.record(transaction=...)` for psycopg and SQLAlchemy, with a multi-process safe relay.
- Metrics: `get_meter()` on OpenTelemetry with a label policy and per-metric cardinality limit.
- Integrations: FastAPI/Starlette (ASGI), Flask and Django middleware, `instrument_engine` for SQLAlchemy and `instrument_celery`.
- Integration guide, deployment guide and a containerised example with an OpenTelemetry Collector.
- Documentation (MkDocs), a runnable FastAPI example and a local OpenTelemetry Collector compose file.
- Package scaffold, tooling, CI workflows and public-API snapshot test.
- `configure()`, `shutdown()` and `ashutdown()` with idempotent configuration and bounded, ordered shutdown.
- `ObservabilityConfig`: validated settings from code, `PYOBS_*` and `OTEL_*` environment variables, `pyobservability.toml` and `[tool.pyobservability]`, in that precedence.
- Security lock: environment variables and config files cannot weaken security settings without an explicit grant.
- Plugin `Registry` with entry-point discovery and OpenTelemetry provider-mode detection.
- `context`: copy-on-write request/tenant/user attributes (`set`, `get`, `snapshot`, `clear`, `scope`) isolated per task, plus `RequestContextBinder` for validated request IDs and tenant headers.
- `Redactor`: fail-closed masking of secrets (by key and value pattern, including Luhn-checked card numbers) and PII (mask, keyed hash or drop) across nested structures, with depth, size and cycle limits.
- Tracing: `get_tracer()` and `@traced` (sync and async) on the OpenTelemetry SDK with service resource, configurable sampling, batching, and console, OTLP gRPC/HTTP or plugin span exporters; span names, attributes, events, links and status are redacted before export. Tracers stay valid across `reconfigure` and `shutdown`.
- Provider modes: own the global provider, attach a detachable processor to an existing SDK provider, or stay external.
- W3C trace propagation helpers `inject_trace_headers` / `context_from_headers`.
- `instrument()` / `uninstrument()` for `requests` and `httpx` (extra: `http`), duplicate-safe.
- `logging.export_to_otel`: redacted OpenTelemetry log export correlated with the active span.
- `logging.capture_unhandled_exceptions`: logs uncaught process and thread exceptions, then chains to the previous hooks.
- Secret values in URL query strings (`?access_token=…`) are masked everywhere.
- `MissingExtraError` names the extra to install when an optional dependency is absent.
- Metrics: `get_meter()` with counters, up-down counters, histograms (latency buckets by default for unit `s`), gauges and observable instruments, exported through console, OTLP or plugin exporters. Instruments stay valid across `reconfigure` and `shutdown`.
- Metric label policy: secret, PII and per-request labels (`user_id`, `request_id`, `trace_id`, …) are stripped (warned once) or rejected; string label values are redacted; `allowed_label_keys` and `forbidden_label_keys` customise it; cardinality is capped per metric with the standard `otel.metric.overflow` series.
- Self-metric `pyobs.export.errors` counts failed trace and log export batches.
- Audit engine: `audit.record()` / `audit.arecord()` / `audit.flush()` produce schema-validated `AuditEvent`s (UUIDv7 IDs, actor, tenant, resource, action, `changes`, `metadata`, trace and request correlation, source service) enriched from context and redacted before persistence.
- Audit durability boundaries: `memory` (accepted), `disk` (fsynced write-ahead log, recovered after crashes including `kill -9`) and `exporter` (delivered inline before returning).
- Audit delivery: bounded admission that blocks or refuses but never drops, a background dispatcher, per-event retryable/permanent failures, jittered exponential backoff, dead letters to the log or a private JSON-lines file, an `on_delivery_failure` callback, and `pyobs.audit.*` self-metrics.
- Audit exporters: `console`, `file` (JSON lines, owner-only), and plugins via the `pyobservability.audit_exporters` entry point.
- HTTP integrations: pure ASGI `ObservabilityMiddleware` (FastAPI, Starlette, any ASGI app), Flask `PyObservability(app)` (extra: `flask`) and Django `ObservabilityMiddleware` (sync and async) plus an app config reading `settings.PYOBSERVABILITY` (extra: `django`). Each request gets bound request/tenant context, W3C trace continuation, a SERVER span named by route template, `http.server.request.duration` and `http.server.active_requests` metrics, one access log, and an `X-Request-ID` response header; health-check paths are excluded by default.
- FastAPI's native OpenTelemetry telemetry is detected and not duplicated; unhandled exceptions are still recorded on its span.
- Unknown HTTP methods are normalised to `_OTHER`, and ID-like path segments in `url.path` are templated (`/claims/{id}`) for all exported spans.
- Tamper-evident audit chains: per-tenant SHA-256 or HMAC-SHA256 hash chains with key rotation (`auditing.integrity`), and `audit.verify(tenant_id)`, which detects edited, deleted, reordered or re-chained records and is itself audited.
- PostgreSQL audit exporter (extra: `postgres`): append-only table enforced by triggers, chain computed under a row lock so concurrent processes share one gap-free chain, idempotent on `event_id`, rejected rows isolated from the rest of the batch, DSN from options or an environment variable.
- Transactional outbox (`durability.boundary="outbox"`, extra: `postgres`): `audit.record(..., transaction=connection_or_session)` writes the event inside the caller's database transaction, so it commits or rolls back with the business change; a background relay delivers committed rows with leases and `SKIP LOCKED`, safe across processes and crashes.
- SQLAlchemy integration (extra: `sqlalchemy`): `instrument_engine(engine)` adds CLIENT spans and `db.client.operation.duration` without ever capturing bound parameters, an optional slow-query warning, and lets a `Session` or `Connection` carry outbox writes.
- Celery integration (extra: `celery`): `instrument_celery()` carries the trace, request ID, tenant and user through task headers, gives each run a CONSUMER span, context scope and `messaging.process.duration`, and never records task arguments.
- Kafka audit exporter (extra: `kafka`): `acks=all` idempotent producer keyed by tenant with `event_id`/`event_type` headers; failures retry unless the message itself is invalid.
- `get_logger()`: structured JSON or console logs enriched with service, context and trace IDs, redacted before any handler sees them; stdlib `logging` records share the pipeline; a redacted stderr fallback covers logging before `configure()`.
