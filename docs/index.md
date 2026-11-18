# PyObservability

One SDK for logs, traces, metrics and audit events in Python services. It sits on OpenTelemetry and
structlog and adds a single `configure()` call, request context, secret and PII redaction, and an
audit engine with explicit delivery guarantees.

```python
import pyobservability
from pyobservability import audit, get_logger, get_meter, traced

pyobservability.configure(
    service_name="claims-service",
    environment="production",
    tracing={"endpoint": "http://collector:4317"},
    metrics={"endpoint": "http://collector:4317"},
    auditing={
        "durability": {"boundary": "disk", "spool_dir": "/var/lib/audit"},
        "retry": {"dead_letter": "file"},
    },
)

get_logger(__name__).info("claim approved", claim_id="CLM-1")


@traced
def decide(claim_id: str) -> str: ...


audit.record(
    event_type="CLAIM_APPROVED", resource_type="CLAIM", resource_id="CLM-1", action="APPROVE"
)
```

## Install

```bash
uv add pyobservability                       # core: logging, tracing, metrics, audit (memory/disk)
uv add "pyobservability[otlp]"               # OTLP exporters
uv add "pyobservability[fastapi]"            # also: flask, django, sqlalchemy, celery, http
uv add "pyobservability[postgres]"           # PostgreSQL audit store, hash chains, outbox
uv add "pyobservability[kafka]"              # Kafka audit exporter
```

Optional features raise `MissingExtraError` naming the extra to install.

## Two systems, on purpose

| | Telemetry (logs, traces, metrics) | Audit events |
|---|---|---|
| Loss | May be sampled, batched and dropped | Never silently lost |
| Backpressure | Bounded queues, drops are counted | `record()` blocks or raises |
| Delivery | Best effort over OTLP | At-least-once, idempotent on `event_id` |
| Failure | Counted in `pyobs.export.errors` | Retried, then dead-lettered and reported |
