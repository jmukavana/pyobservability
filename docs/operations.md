# Operations

## Start and stop

```python
pyobservability.configure(...)  # once per process; repeat calls with equal settings are no-ops
report = pyobservability.shutdown()  # or: await pyobservability.ashutdown()
report.succeeded  # per-component errors and overruns are listed in report.components
```

`shutdown()` flushes in reverse start order within `shutdown_timeout_s`; an `atexit` hook calls it
for you. Start order is context → metrics → logging → tracing → audit, so audit drains while logging
is still up, and metrics stop last so shutdown-time export failures are still counted.

## Worker processes

Call `configure()` **in each worker process**: after `fork()`, in gunicorn's `post_fork` hook or
Celery's `worker_process_init`. Audit recording in a process that inherited a configured SDK raises
`AuditDurabilityError` rather than queueing events no thread will deliver. Give each process its own
spool directory (or use the outbox, which several processes can share).

## Existing OpenTelemetry setups

`provider_mode="auto"` owns the global tracer provider when none exists, attaches a redacting
processor to an existing SDK provider, and otherwise leaves OpenTelemetry alone. An SDK meter
provider installed first is left untouched, because OpenTelemetry cannot add exporters to it.

## Local Collector

`examples/collector/docker-compose.yml` runs an OpenTelemetry Collector that prints what it receives;
`examples/fastapi/app.py` is a complete service to point at it.

## Performance

Measured on a developer laptop (Python 3.14, results vary by ±30% between runs), against the targets in
the design spec:

| Operation | Measured | Target |
|---|---|---|
| Redact a 10-field event | ~30 µs | < 50 µs |
| Structured log call, 10 fields, to a file | ~85 µs | < 100 µs |
| HTTP middleware: context + request ID | ~20 µs | |
| HTTP middleware: + span and metrics | ~55 µs | < 150 µs |
| HTTP middleware: + access log | ~190 µs | < 150 µs |
| `audit.record`, memory boundary | ~30 µs (~32,000/s) | |
| `audit.record`, disk boundary with fsync | ~75 µs (~13,500/s) | |
| Counter `add()` with three labels | ~3 µs | |

The access log is one full structured log record, which is why it misses the middleware target;
set `http.access_log=False` if you already log requests elsewhere. Redacting hostile 16 KB strings
takes at most ~30 ms and scales linearly with input size.

## Support matrix

Python 3.12, 3.13 and 3.14 are tested; the lowest allowed dependency versions are tested in CI. The
OpenTelemetry package versions that were verified together are recorded in `uv.lock`.
