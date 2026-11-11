# PyObservability

Unified observability and auditing SDK for Python: structured logging, distributed tracing, metrics, request/tenant context, secret and PII redaction, and durable audit events — built on OpenTelemetry and usable from FastAPI, Django, Flask, Celery or plain Python.

> **Status:** pre-alpha. The package scaffold exists; features land phase by phase.

## Development

```bash
uv sync
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run mypy src/
uv run lint-imports
```

Install the git hooks once with `uv run pre-commit install`.
