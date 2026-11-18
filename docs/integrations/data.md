# Databases, Celery and Kafka

## SQLAlchemy (extra `sqlalchemy`)

```python
from pyobservability.integrations.sqlalchemy import instrument_engine

engine = instrument_engine(create_engine(url), slow_query_threshold_s=0.5)
```

A CLIENT span per statement, nested under the current span, and `db.client.operation.duration`
labelled by database system and operation only. Statement text is captured (truncated to 2 KiB);
**bound parameters never are**. Failures are recorded on the span; slow statements log a warning.
Importing this module also lets `audit.record(transaction=session)` write to the
[outbox](../auditing.md#transactional-outbox). Async sessions are not supported by the outbox yet.

## Celery (extra `celery`)

Call `instrument_celery()` in both the publisher and the worker (for example at import of your Celery app).

- Publishing adds `traceparent` and the request, tenant and user IDs to the task headers.
- Each run gets a CONSUMER span (`process <task name>`), the same context, and
  `messaging.process.duration`; failures are recorded on both.
- Task arguments are never recorded. Tasks started without context get a fresh request ID.

With the prefork pool call `pyobservability.configure()` in `worker_process_init`, not before forking.

## Kafka (extra `kafka`)

The `kafka` [audit exporter](../auditing.md#exporters) publishes JSON events with `acks=all` and an
idempotent producer, keyed by tenant so each tenant's events stay ordered, with `event_id`,
`event_type` and `schema_version` headers. Only invalid-message errors (for example, too large) are
permanent; everything else retries. Consumers should de-duplicate on `event_id`: delivery is
at-least-once.
