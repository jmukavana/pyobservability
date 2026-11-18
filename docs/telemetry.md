# Tracing and metrics

Both are the native OpenTelemetry API; PyObservability installs the providers, resource
(`service.name`, `service.version`, `deployment.environment.name`), exporters and policies.

## Tracing

```python
from pyobservability import get_tracer, traced


@traced  # sync or async; also @traced(name=..., attributes=...)
async def process(claim_id: str) -> None: ...


with get_tracer(__name__).start_as_current_span("approve") as span:
    span.set_attribute("claim.id", "CLM-1")
```

- Sampling: `sampler="parent_based_ratio"` with `sample_rate`, or `always_on` / `always_off`. Sampling
  never applies to audit events.
- Exporters: `console`, `otlp` (gRPC or HTTP), `none`, or a plugin registered under the
  `pyobservability.span_exporters` entry-point group.
- Every exporter is wrapped so span names, attributes, events, links and error descriptions are
  [redacted](security.md) first, and `url.path` has ID-like segments templated.
- Propagation is W3C Trace Context: `from pyobservability.tracing import inject_trace_headers,
  context_from_headers`.
- `instrument("requests", "httpx")` traces outgoing calls (extra `http`); `uninstrument(...)` undoes it.

Tracers and instruments obtained **before** `configure()`, or kept across `reconfigure`/`shutdown`,
keep working: OpenTelemetry allows setting the global provider once, so PyObservability installs a
delegating provider once and repoints it.

## Metrics

```python
meter = get_meter("claims")
approved = meter.create_counter("claims.approved", unit="{claim}")
duration = meter.create_histogram("claims.duration", unit="s")  # latency buckets by default
approved.add(1, {"channel": "web"})
```

Counters, up-down counters, histograms, gauges and observable instruments (callbacks must be plain
functions, not generators).

**Labels are policed on every call.** Keys that look like secrets, PII or per-request values
(`user_id`, `request_id`, `trace_id`, `session_id`, `password`, `email`, …) are stripped, with one
warning per label, or rejected with `SecurityPolicyError` if `on_forbidden_label="reject"`.
`allowed_label_keys` turns this into an allow-list; `forbidden_label_keys` extends the block list.
String label values are redacted. Each metric is capped at `max_cardinality_per_metric` series;
beyond that, new label sets collapse into the standard `otel.metric.overflow` series.

## Self-metrics

| Metric | Meaning |
|---|---|
| `pyobs.export.errors` | trace or log export batches an exporter failed to send (`signal` label) |
| `pyobs.audit.events.recorded` / `delivered` / `failed` | audit event outcomes |
| `pyobs.audit.delivery.latency` | recorded → delivered, seconds |
| `pyobs.audit.queue.depth` | events accepted but not yet delivered |

Metric export failures are not counted: the counter itself could not be exported.
