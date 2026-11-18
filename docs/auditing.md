# Auditing

Audit events are records of who did what to which resource. They have their own schema, delivery
path and failure handling, separate from logs and traces.

```python
from pyobservability import audit

receipt = audit.record(
    event_type="CLAIM_APPROVED",  # UPPER_SNAKE_CASE
    resource_type="CLAIM",
    resource_id="CLM-001",
    action="APPROVE",
    actor_id="USR-1001",
    tenant_id="tenant-001",  # default to the request context
    changes={"status": {"old": "PENDING", "new": "APPROVED"}},
    metadata={"channel": "web"},
)
await audit.arecord(...)  # same, run in a worker thread so the event loop is not blocked
audit.flush(timeout_s=10)  # wait until everything accepted was delivered or dead-lettered
```

Validation errors raise `AuditValidationError` to the caller. Recording without `configure()` raises
`NotConfiguredError`; audit never discards silently. `changes` and `metadata` are
[redacted](security.md) before storage and limited to 64 KiB and 6 levels of nesting.

## What a normal return guarantees

| `durability.boundary` | `record()` returns after… | `receipt.durability` |
|---|---|---|
| `memory` | admission to a bounded queue (**lost on a crash**) | `accepted` |
| `disk` | an fsynced append to a write-ahead log in `spool_dir` | `spooled` |
| `exporter` (with `delivery="sync"`) | every required exporter accepted it | `delivered` |
| `outbox` (extra `postgres`) | the row was written in your database transaction | `stored` |

`memory` is blocked in `production` unless you set `allow_memory_durability=True`.
`delivery="sync"` and `boundary="exporter"` must be used together.

## Delivery

- **At-least-once, idempotent on `event_id`.** After a crash, unacknowledged events are redelivered.
- **Bounded, never dropping.** When `queue_max_size` events are in flight, `record()` blocks up to
  `block_timeout_s` (or raises at once with `on_queue_full="raise"`) and then raises
  `AuditDurabilityError`.
- **Retries.** Exponential backoff with jitter (`retry.*`). Exporters report failures per event as
  retryable or permanent.
- **Dead letters.** Events that cannot be delivered go to the log (`dead_letter="log"`) or to
  `<spool_dir>/dead-letter.jsonl` (`"file"`), call `on_delivery_failure(event, reason)` and count in
  `pyobs.audit.events.failed`. Events still in memory when shutdown times out are dead-lettered too.

## Exporters

| Name | Notes |
|---|---|
| `console` | JSON lines on stdout; development only |
| `file` | JSON lines, owner-only permissions; option `path` |
| `postgres` | append-only table, hash chain per tenant; options `dsn` / `dsn_env`, `schema` |
| `kafka` | `acks=all`, idempotent producer, keyed by tenant; options `bootstrap_servers`, `topic` |
| plugin | implement `AuditExporter` (`name`, `export`, `shutdown`; thread-safe) and register under the `pyobservability.audit_exporters` entry point |

Configure with `auditing={"exporters": [{"name": "postgres", "options": {...}, "required": True}]}`.

## Tamper evidence

With the `postgres` exporter, each tenant's events form a hash chain computed by the database under a
row lock. Add HMAC keys so a database administrator cannot recompute it:

```python
auditing = {"integrity": {"keys": {"2026-11": "…secret…"}, "active_key_id": "2026-11"}}
audit.verify("tenant-001")  # VerifyResult(ok, checked, first_bad_seq, problem)
```

It detects edited, deleted, reordered and re-chained records, and is itself recorded as an
`AUDIT_CHAIN_VERIFIED` access event. The table also rejects `UPDATE`, `DELETE` and `TRUNCATE` with
triggers. Keep old keys in `keys` after rotating so older records still verify.

## Transactional outbox

```python
auditing = {"durability": {"boundary": "outbox", "outbox": {"dsn_env": "DATABASE_URL"}}}

with Session(engine) as session, session.begin():
    session.add(claim)
    audit.record(..., transaction=session)  # commits or rolls back with the claim
```

`transaction` may be a psycopg connection, or a SQLAlchemy `Session`/`Connection` once
`pyobservability.integrations.sqlalchemy` is imported. A background relay delivers committed rows;
it is safe to run in several processes and recovers rows leased by a crashed relay.

## Processes

Delivery threads do not survive `fork()`. If you configure before forking (gunicorn `--preload`,
Celery prefork), `audit.record()` in the child raises `AuditDurabilityError` instead of queueing events
nobody will deliver. Call `configure()` in each worker (gunicorn `post_fork`, Celery
`worker_process_init`). A disk spool directory belongs to one process at a time.
