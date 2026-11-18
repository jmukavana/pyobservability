# PyObservability

Unified observability and auditing SDK for Python: structured logs, distributed traces, metrics,
request and tenant context, secret and PII redaction, and durable audit events. Built on OpenTelemetry
and structlog; works with FastAPI, Starlette, Flask, Django, SQLAlchemy, Celery and plain Python.

> **Status:** pre-release (0.1.0 in development). The API in `pyobservability.__all__` is the public
> surface.

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
audit.record(
    event_type="CLAIM_APPROVED", resource_type="CLAIM", resource_id="CLM-1", action="APPROVE"
)
```

- **Secure by default:** one redactor on every egress path; environment variables and files cannot
  weaken it.
- **Audit is not telemetry:** its own schema, a durability boundary you choose, retries, dead letters,
  an optional transactional outbox and tamper-evident hash chains.
- **Small public API, pluggable exporters**, optional extras for everything heavy.

See [`docs/`](docs/index.md) (build with `mkdocs serve` after `pip install mkdocs-material`) and the
runnable [`examples/`](examples/fastapi/app.py).

## Extras

`otlp`, `http`, `fastapi`, `flask`, `django`, `sqlalchemy`, `celery`, `postgres`, `kafka`.

## Development

```bash
uv sync
uv run pytest                              # the PostgreSQL tests start a throwaway local server
uv run ruff check . && uv run ruff format --check .
uv run mypy src/ tests/
uv run lint-imports
```

Install the git hooks once with `uv run pre-commit install`. `KAFKA_TEST_BOOTSTRAP=host:9092` enables
the live-broker Kafka test.
