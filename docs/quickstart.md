# Integration guide

This guide takes an existing Python service from nothing to logs, traces, metrics and audit events
flowing to your backends. Every snippet was run against this version. Work through the steps in
order; each ends with a check you can run.

**Prerequisites:** Python 3.12 or newer, and an OpenTelemetry Collector (or any OTLP endpoint) for
production. For local work you need nothing: the defaults print to the console.

## What you get

| Signal | What happens automatically | What you add |
|---|---|---|
| Logs | One JSON format for your logs and for stdlib `logging` from third-party libraries, with trace, request and tenant IDs on every line, and secrets and PII redacted | `get_logger(__name__).info(...)` |
| Traces | A SERVER span per HTTP request, continued from an incoming `traceparent` | `@traced` on functions you care about |
| Metrics | HTTP request duration and active requests | `get_meter(...)` for business metrics |
| Audit | Nothing is automatic | `audit.record(...)` at each business action |

Telemetry (logs, traces, metrics) may be sampled or dropped under pressure. **Audit events are never
silently dropped**, and they follow a separate, durable path. See [Auditing](auditing.md).

## Integration checklist

1. [Install](#1-install) the package with the extras you need.
2. [Choose the audit durability](#2-decide-where-settings-live) (required in production).
3. [Configure once at startup](#3-configure-at-startup) and hook in your framework.
4. [Instrument your code](#5-instrument-your-code): logs, spans, metrics, context, audit.
5. [Point it at your backends](#6-connect-your-backends).
6. [Verify locally](#7-verify-it-works), then [in staging](#8-rollout-checklist).

## 1. Install

```bash
uv add "pyobservability[fastapi,otlp]"      # or: pip install "pyobservability[fastapi,otlp]"
```

| Extra | Needed for |
|---|---|
| `otlp` | Exporting traces, metrics and logs to a Collector. Required for production telemetry |
| `fastapi`, `flask`, `django` | The matching web integration |
| `sqlalchemy` | `instrument_engine` and the transactional outbox through SQLAlchemy |
| `celery` | `instrument_celery` |
| `postgres` | PostgreSQL audit store, hash chains and outbox |
| `kafka` | Kafka audit exporter |
| `http` | Tracing for outgoing `requests` calls |
| `all` | Everything above |

Using a feature without its extra raises `MissingExtraError` naming the extra to install.

## 2. Decide where settings live

Settings are merged from five sources. Higher beats lower:

1. Arguments to `configure()`.
2. `PYOBS_*` environment variables. Nest with a double underscore: `PYOBS_TRACING__SAMPLE_RATE=0.1`.
3. Standard `OTEL_SERVICE_NAME`, `OTEL_RESOURCE_ATTRIBUTES` and `OTEL_EXPORTER_OTLP_[TRACES_|METRICS_]ENDPOINT`.
4. `pyobservability.toml` in the working directory (or the file named by `PYOBS_CONFIG_FILE`).
5. `[tool.pyobservability]` in `pyproject.toml`.

**Recommended split:** keep stable, non-secret settings in code or a TOML file checked into the repo,
and put per-environment values (endpoints, environment name, spool path, credentials) in environment
variables so the same image runs everywhere.

A complete production `pyobservability.toml`:

```toml
service_name = "claims-service"
service_version = "1.4.2"
environment = "production"

[tracing]
endpoint = "http://otel-collector:4317"
sample_rate = 0.1            # keep 10% of traces; audit is never sampled

[metrics]
endpoint = "http://otel-collector:4317"

[logging]
level = "INFO"
export_to_otel = true        # also send logs over OTLP, linked to the active span

[auditing.durability]
boundary = "disk"
spool_dir = "/var/lib/claims/audit"

[auditing.retry]
dead_letter = "file"

[http]
excluded_paths = ["/healthz", "/readyz"]   # no spans, metrics or access logs for these
```

The same thing as environment variables:

```bash
PYOBS_SERVICE_NAME=claims-service
PYOBS_ENVIRONMENT=production
OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4317   # the endpoint; OTLP is the default exporter outside development
PYOBS_AUDITING__DURABILITY__BOUNDARY=disk
PYOBS_AUDITING__DURABILITY__SPOOL_DIR=/var/lib/claims/audit
```

### Defaults you should know

| | `environment="development"` (default) | any other environment |
|---|---|---|
| Trace and metric exporter | `console` | `otlp` (an endpoint is then required) |
| Audit held only in memory | allowed, with a warning | **refused**: `configure()` raises `ConfigurationError` |
| Log format | JSON to stdout | JSON to stdout |

### Pick an audit durability boundary

This is the one decision production forces on you. Choose by what your service already has:

| You have | Use | Why |
|---|---|---|
| A PostgreSQL database and want audit committed with your business changes | `outbox` | The event is written in your transaction and relayed afterwards. No audit event for a rolled-back change, none lost for a committed one |
| A persistent disk (VM, StatefulSet volume) | `disk` | An fsynced write-ahead log survives a crash; one process owns each directory |
| A durable sink reachable synchronously (Kafka, PostgreSQL) | `exporter` with `delivery="sync"` | `record()` returns only after the sink accepted the event; slower per call |
| Nothing, and loss is acceptable (prototype) | `memory` | Lost on a crash. In production it also needs `allow_memory_durability=True` |

Details and trade-offs: [Auditing](auditing.md#what-a-normal-return-guarantees).

## 3. Configure at startup

Call `pyobservability.configure(...)` **once per process, as early as possible**, before you create
loggers you care about, and before serving traffic. Repeating the call with identical settings is a
no-op; different settings raise `AlreadyConfiguredError` unless you pass `reconfigure=True`.

Only `service_name` is required. With everything else in the environment or a TOML file:

```python
import pyobservability

pyobservability.configure(service_name="claims-service")
```

The sections below show where that call goes for each kind of application.

### FastAPI, Starlette and other ASGI apps

```python
from contextlib import asynccontextmanager

from fastapi import FastAPI

import pyobservability
from pyobservability.integrations.fastapi import ObservabilityMiddleware


@asynccontextmanager
async def lifespan(_: FastAPI):
    pyobservability.configure(service_name="claims-service")
    yield
    await pyobservability.ashutdown()  # flushes spans, metrics, logs and audit


app = FastAPI(lifespan=lifespan)
app.add_middleware(ObservabilityMiddleware)
```

Run it with `uvicorn module:app`. A full runnable service is `examples/fastapi/app.py`. Newer FastAPI
versions trace requests natively; the middleware detects that and avoids double spans.

### Flask

```python
import pyobservability
from flask import Flask
from pyobservability.integrations.flask import PyObservability

pyobservability.configure(service_name="claims-service")  # before creating routes that log

app = Flask(__name__)
PyObservability(app)  # or: ext = PyObservability(); ext.init_app(app)
```

Installing it twice on one app is harmless. Flask has no shutdown hook, so rely on the built-in
`atexit` flush, or call `pyobservability.shutdown()` from your own teardown.

### Django

```python
# settings.py
INSTALLED_APPS = [
    "pyobservability.integrations.django",  # calls configure() from PYOBSERVABILITY below
    # ...
]

MIDDLEWARE = [
    "pyobservability.integrations.django.ObservabilityMiddleware",  # keep it first
    # ...
]

PYOBSERVABILITY = {
    "service_name": "claims-service",
    "environment": "production",
}
```

Sync and async views both work. If `PYOBSERVABILITY` is absent, the app does not configure anything,
so you can call `configure()` yourself (for example from `manage.py`/`wsgi.py`) or use environment
variables with a one-line `configure(service_name=...)`.

### Workers, scripts and anything without HTTP

```python
import pyobservability
from pyobservability import get_logger


def main() -> None:
    pyobservability.configure(service_name="claims-batch")
    try:
        run_job()
    finally:
        pyobservability.shutdown()  # drains audit and exports before the process exits


main()
```

Use `context.scope(...)` around each unit of work so its logs and audit events carry the right IDs:

```python
from pyobservability import context

with context.scope(request_id="job-2026-11-18", tenant_id="tenant-001"):
    process(claim)
```

### Celery

```python
from celery import Celery
from celery.signals import worker_process_init

import pyobservability
from pyobservability.integrations.celery import instrument_celery

app = Celery("claims")
instrument_celery()  # in the publisher AND the worker; repeats are no-ops


@worker_process_init.connect
def configure_observability(**_):
    pyobservability.configure(service_name="claims-worker")  # per prefork child, not before forking
```

The publisher adds `traceparent` and the request, tenant and user IDs to each task's headers; the
worker opens a CONSUMER span and restores the context, so one trace spans the web request and the
background task. Task arguments are never recorded.

### SQLAlchemy

```python
from sqlalchemy import create_engine
from pyobservability.integrations.sqlalchemy import instrument_engine

engine = instrument_engine(create_engine(DATABASE_URL), slow_query_threshold_s=0.5)
```

Each statement becomes a CLIENT span under the current request span, with the statement text (truncated
to 2 KiB) but **never bound parameters**. Statements slower than the threshold log a warning.

### Outgoing HTTP calls

With the `http` extra, `pyobservability.instrument("requests", "httpx")` traces outgoing calls and
propagates `traceparent`; repeats are skipped. Undo with `pyobservability.uninstrument(...)`.

## 4. Servers that fork (gunicorn, uWSGI, Celery prefork)

Background delivery threads do not survive `fork()`. Configure in each worker process, not in the
master:

```python
# gunicorn.conf.py
def post_fork(server, worker):
    import pyobservability

    pyobservability.configure(service_name="claims-service")
```

Do not use `--preload`. Audit recording in a process that inherited a configured SDK raises
`AuditDurabilityError` rather than queue events nobody would deliver. With `boundary="disk"`, give
each process its own `spool_dir`; with `boundary="outbox"`, workers share one store. More in
[Deployment](deployment.md).

## 5. Instrument your code

### Logs

```python
from pyobservability import get_logger

log = get_logger(__name__)
log.info("claim approved", claim_id="CLM-001", amount=1200)
```

Pass data as keyword arguments, not formatted into the message, so it stays searchable. Existing
`logging.getLogger(...)` calls in your code and in libraries keep working and share the format.
`Authorization` headers, tokens, passwords, card numbers and similar are redacted automatically;
see [Security](security.md) for the rules and how to extend them.

### Spans

```python
from pyobservability import get_tracer, traced


@traced  # span named after the function
def decide(claim_id: str) -> str: ...


@traced(name="claims.score", attributes={"model": "v3"})
async def score(claim_id: str) -> float: ...  # async functions work too


tracer = get_tracer(__name__)  # a native OpenTelemetry tracer
with tracer.start_as_current_span("load-policy") as span:
    span.set_attribute("policy.id", policy_id)
```

Exceptions are recorded on the span and re-raised. Pass `record_exceptions=False` to opt out.

### Metrics

```python
from pyobservability import get_meter

approved = get_meter("claims").create_counter("claims.approved", unit="{claim}")
approved.add(1, {"channel": "api"})
```

Metric labels pass through a policy: keep them low-cardinality (channel, status), never IDs or
emails. Forbidden label keys are stripped (or rejected, with `metrics.on_forbidden_label="reject"`), and
each metric is capped at `metrics.max_cardinality_per_metric` series (2000 by default).

### Context: who and which tenant

```python
from pyobservability import context

context.set(user_id=current_user.id, tenant_id=current_user.tenant)  # for the rest of this request
```

Everything logged or audited afterwards in the same request carries these IDs. The web integrations
set `request_id` for you, taken from the `X-Request-ID` header when it is 8 to 128 characters of
`A-Za-z0-9._-`, otherwise freshly generated, and echo it back on the response. To take the tenant
from a request header, set `context={"tenant_header": "X-Tenant-ID"}`.

### Audit events

```python
from pyobservability import audit

audit.record(
    event_type="CLAIM_APPROVED",  # UPPER_SNAKE_CASE, 3 to 64 characters
    resource_type="CLAIM",
    resource_id="CLM-001",
    action="APPROVE",
    actor_id="USR-1001",
    changes={"status": {"old": "PENDING", "new": "APPROVED"}},
)
```

In async code use `await audit.arecord(...)`. Record **after** the action succeeds, and inside the
same transaction when you use the outbox:

```python
with Session(engine) as session, session.begin():
    claim.status = "APPROVED"
    audit.record(
        event_type="CLAIM_APPROVED",
        resource_type="CLAIM",
        resource_id=claim.id,
        action="APPROVE",
        transaction=session,
    )
```

A normal return means the configured durability boundary was reached. Consumers of audit data must
de-duplicate on `event_id`, because delivery is at-least-once.

## 6. Connect your backends

### Telemetry: through a Collector

Send OTLP to a Collector and let it fan out to whatever you use. This keeps credentials and
vendor choice out of every service. A minimal Collector configuration that receives from the SDK and
forwards to any OTLP-compatible backend:

```yaml
receivers:
  otlp:
    protocols:
      grpc: {endpoint: 0.0.0.0:4317}
      http: {endpoint: 0.0.0.0:4318}
processors:
  batch: {}
exporters:
  otlphttp:
    endpoint: https://your-backend.example.com
    headers: {authorization: "Bearer ${env:BACKEND_TOKEN}"}
service:
  pipelines:
    traces:  {receivers: [otlp], processors: [batch], exporters: [otlphttp]}
    metrics: {receivers: [otlp], processors: [batch], exporters: [otlphttp]}
    logs:    {receivers: [otlp], processors: [batch], exporters: [otlphttp]}
```

Replace the exporter with your vendor's (Datadog, Grafana, Honeycomb, Jaeger and others all ship
Collector exporters). For a local Collector that prints what it receives, use
`examples/collector/docker-compose.yml`.

The SDK talks to the Collector over gRPC by default (`protocol="grpc"`, port 4317). Use
`tracing.protocol="http"` with port 4318 if gRPC is blocked. An `https://` endpoint always uses TLS.
If your app already configures OpenTelemetry, see
[Existing OpenTelemetry setups](operations.md#existing-opentelemetry-setups).

### Audit: where events end up

Choose one or more exporters; see [Auditing](auditing.md#exporters) for options.

```python
pyobservability.configure(
    service_name="claims-service",
    environment="production",
    auditing={
        "durability": {"boundary": "outbox", "outbox": {"dsn_env": "DATABASE_URL"}},
        "exporters": [
            {"name": "postgres", "required": True, "options": {"dsn_env": "AUDIT_DATABASE_URL"}},
            {"name": "kafka", "options": {"bootstrap_servers": "kafka:9092", "topic": "audit"}},
        ],
    },
)
```

Credentials such as `dsn_env` are read from the named environment variable and never logged.

## 7. Verify it works

Verify locally before touching staging. With defaults (`environment="development"`) everything
prints to the console.

1. Start your service and make a request:
   ```bash
   curl -i -X POST localhost:8000/claims/C-1/approve -H 'X-Request-ID: local-test-0001'
   ```
2. Check the response carries `x-request-id: local-test-0001`.
3. Check the log output has an application line (`"message": "claim approved"`) and an access line
   (`"logger": "pyobservability.access"`) that share the same `trace_id` and `request_id`.
4. Check a span printed with `"kind": "SpanKind.SERVER"` and `http.route` set to your route
   *template*, not the concrete path.
5. Check the audit event printed (console exporter) or landed in your sink, with the same `trace_id`.
6. Hit `/healthz`: it produces no span and no access log.

To check the Collector path, run `examples/collector/docker-compose.yml`, set
`OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317` together with `PYOBS_TRACING__EXPORTER=otlp` and
`PYOBS_METRICS__EXPORTER=otlp` (in `development` the endpoint alone leaves the exporters on
`console`), repeat the request, and look for your `service.name` in the Collector log. Spans are
batched, so allow up to about 5 seconds. For a full dashboard instead of Collector logs, see
[Grafana stack](integrations/grafana.md).

### Testing your own code

Call `configure(service_name="test", auditing={"allow_memory_durability": True},
tracing={"exporter": "none"}, metrics={"exporter": "none"}, reconfigure=True)` in a fixture and
`shutdown()` afterwards. In-memory audit still emits a `PyObservabilityWarning`; if your test run
treats warnings as errors, add
`filterwarnings = ["ignore::pyobservability.PyObservabilityWarning"]` to your pytest configuration.
Keep `PYOBS_*` and `OTEL_*` variables out of the test environment, or tests may pick up real settings.

## 8. Rollout checklist

- [ ] `environment` is set to something other than `development` in deployed environments.
- [ ] Audit durability is `outbox`, `disk` or `exporter`, and the volume or database is provisioned.
- [ ] `tracing.sample_rate` chosen deliberately (audit ignores it).
- [ ] Health endpoints are in `http.excluded_paths`.
- [ ] `configure()` runs in every worker process, not before `fork()`.
- [ ] The process gets `SIGTERM` and exits normally, and the grace period exceeds
      `shutdown_timeout_s` (10 s by default), so `shutdown()` can flush.
- [ ] Downstream audit consumers de-duplicate on `event_id`.
- [ ] Dashboards or alerts on `pyobs.export.errors` and `pyobs.audit.events.failed`.
- [ ] No secrets or personal data placed in span names, metric labels or log *messages* (put them in
      keyword fields, where redaction applies).

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `ConfigurationError: in production, audit events must not be held only in memory` | Set `auditing.durability.boundary` to `disk`, `exporter` or `outbox` (see [step 2](#pick-an-audit-durability-boundary)) |
| `ConfigurationError: endpoint is required when exporter='otlp'` | Non-development environments default to `otlp`. Set `tracing.endpoint` and `metrics.endpoint`, or `OTEL_EXPORTER_OTLP_ENDPOINT`, or choose another exporter |
| `ConfigurationError ... Extra inputs are not permitted` | A misspelled or unknown setting. Unknown keys are rejected on purpose |
| `AlreadyConfiguredError` | `configure()` ran twice with different settings. Call it once, or pass `reconfigure=True` |
| `NotConfiguredError` from `audit.record` | Audit ran before `configure()`, or with auditing disabled. Audit never discards silently |
| `AuditValidationError` | Invalid event, for example `event_type` not `UPPER_SNAKE_CASE`, or `changes` over 64 KiB or 6 levels deep |
| `AuditDurabilityError` in a worker | The SDK was configured before `fork()`. Configure in `post_fork` / `worker_process_init`. It is also raised when the audit queue is full and `block_timeout_s` expires |
| `MissingExtraError` | Install the named extra, for example `pip install "pyobservability[kafka]"` |
| Request ID in the response differs from the one I sent | Incoming IDs must be 8 to 128 characters of `A-Za-z0-9._-`; anything else is replaced. Also check `context.trust_incoming_request_id` |
| `PyObservabilityWarning: audit events are held in memory` | The in-memory boundary is in use (the development default, or `allow_memory_durability=True`). Choose a durable boundary to remove it |
| No spans or metrics in the backend | Confirm an `otlp` exporter and endpoint are set, the Collector is reachable, and that you called `shutdown()` or waited for the batch delay in short-lived scripts |
| A health check or `/metrics` request is missing | Expected: paths in `http.excluded_paths` are skipped. Defaults are `/health`, `/healthz`, `/livez`, `/readyz` and `/metrics` |
| Another logging handler sees unredacted text | Redaction applies to `get_logger()` calls and PyObservability's own handlers; a foreign handler still sees raw stdlib `logging` calls. See [Logging](logging.md#behaviour-to-know) |
| Settings in a file or env var are ignored or rejected | Security settings cannot be weakened from env or files. See [Security](security.md#the-security-lock) |

## Where to go next

- [Configuration](configuration.md): every setting.
- [Web frameworks](integrations/web.md) and [Databases, Celery and Kafka](integrations/data.md): integration details.
- [Auditing](auditing.md): guarantees, exporters, integrity and the outbox.
- [Deployment](deployment.md) and [Operations](operations.md): containers, Kubernetes, forking servers, shutdown.
