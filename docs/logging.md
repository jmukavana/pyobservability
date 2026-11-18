# Logging

`get_logger(name)` returns a structlog logger. Standard-library `logging` records from your own code
and third-party libraries go through the same pipeline and handler, so there is one format.

```python
log = get_logger(__name__)
log.info("claim approved", claim_id="CLM-001", tenant_id="tenant-001")
```

```json
{"timestamp": "2026-11-16T10:00:00.000000Z", "level": "INFO", "message": "claim approved",
 "logger": "claims", "service": "claims-service", "environment": "production", "version": "1.0.0",
 "host": "web-1", "pid": 4242, "trace_id": "…32 hex", "span_id": "…16 hex",
 "request_id": "…", "tenant_id": "tenant-001", "claim_id": "CLM-001"}
```

The first four keys and the service metadata are a stable contract.

## Context

```python
from pyobservability import context

with context.scope(user_id="USR-1", tenant_id="tenant-001"):  # also: async with
    log.info("inside")  # carries both IDs
context.set(...)
context.get("tenant_id")
context.snapshot()
context.clear()
```

Context uses one copy-on-write `ContextVar`: concurrent tasks never see each other's values, and
`asyncio.to_thread` carries the caller's context. Plain `threading.Thread` starts empty.

## Behaviour to know

- **Redaction happens before any handler.** Other handlers on the root logger (Sentry, files) only
  ever see redacted records for `get_logger()` calls. Standard-library records are redacted on
  PyObservability's own handlers; a foreign handler still sees the raw `logging` call.
- **Before `configure()`**, warnings and above go to stderr as redacted JSON; lower levels are dropped.
- **Repeated `configure()`** replaces its handler rather than adding another.
- `logging.export_to_otel=True` also sends logs over OTLP as redacted records linked to the active span.
- `logging.capture_unhandled_exceptions=True` logs uncaught process and thread exceptions, then calls
  the hooks that were installed before.
