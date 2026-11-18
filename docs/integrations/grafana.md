# Grafana stack

`examples/grafana/` is a complete local observability stack for the SDK: metrics in Prometheus,
traces in Tempo, logs in Loki, all browsed in Grafana with a ready-made dashboard. The SDK needs no
extra code for it: it sends standard OTLP to an OpenTelemetry Collector, which fans the three signals
out. Everything here was run end to end against the FastAPI example.

```
your service ──OTLP──▶ Collector ──▶ Prometheus (metrics)  ┐
                                 ├─▶ Tempo      (traces)   ├──▶ Grafana
                                 └─▶ Loki       (logs)     ┘
```

## Run it

```bash
docker compose -f examples/grafana/docker-compose.yml up --build
```

Open <http://localhost:3000> and the **PyObservability** dashboard. A small traffic generator calls the
example service every half second, so panels fill within about a minute. Stop and discard everything
with `docker compose -f examples/grafana/docker-compose.yml down -v`.

| Service | Role | Port |
|---|---|---|
| `app` | The FastAPI example, instrumented with the SDK | 8000 |
| `traffic` | Sends requests to `app` so there is data to look at | none |
| `collector` | Receives OTLP and routes each signal to its store | none |
| `prometheus` | Metrics, with its OTLP receiver enabled | 9090 |
| `tempo` | Traces | none |
| `loki` | Logs | none |
| `grafana` | UI with the three datasources and the dashboard provisioned | 3000 |

## Point your own service at it

Three settings are enough. In the default `development` environment the SDK prints to the console, so
the exporters must be switched on explicitly:

```bash
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317   # the Collector (publish its 4317 port first)
PYOBS_TRACING__EXPORTER=otlp
PYOBS_METRICS__EXPORTER=otlp
PYOBS_LOGGING__EXPORT_TO_OTEL=true                  # also ship logs, linked to their trace
```

In any other environment (`PYOBS_ENVIRONMENT=production`) the trace and metric exporters already
default to `otlp`, so only the endpoint is needed. The compose file keeps the Collector private to its
network; add `ports: ["4317:4317"]` to the `collector` service to send from outside it.

## What the dashboard shows

| Panel | Query basis | Read it as |
|---|---|---|
| Requests / s, 5xx ratio, p95 latency, in flight | `http_server_request_duration_seconds_*`, `http_server_active_requests` | Service health at a glance |
| Requests by route and status | the same counter, grouped by `http_route` and status code | Which endpoints are busy or failing; routes are templates, never concrete paths |
| Latency percentiles | histogram buckets, p50, p95, p99 | Slow tail |
| Audit events recorded vs delivered | `pyobs_audit_events_recorded_total`, `pyobs_audit_events_delivered_total` | The lines should match; a growing gap means delivery is behind |
| Audit queue depth | `pyobs_audit_queue_depth` | Events accepted but not yet delivered |
| Audit delivery latency | `pyobs_audit_delivery_latency_seconds_*` | Time from `record()` to delivery |
| Audit events failed | `pyobs_audit_events_failed_total` | **Must stay at zero.** Anything else means dead-lettered events |
| Telemetry export errors | `pyobs_export_errors_total` | Traces, metrics or logs failing to export; telemetry may be dropped |
| Logs | Loki, `service_name` | Structured logs; expand a line and use **Open trace** |
| Recent traces | Tempo, TraceQL | One row per request; open one for its span tree |

The failure counters only exist in Prometheus after the first failure, so their panels use
`or vector(0)` and show zero until then.

### Metric names

OpenTelemetry names become Prometheus names by replacing dots with underscores and appending the unit
(`_seconds`) or `_total` for counters. Resource attributes are promoted to labels
(`service_name`, `service_version`, `deployment_environment_name`), and `job` is the service name.

| In the SDK | In Prometheus |
|---|---|
| `http.server.request.duration` | `http_server_request_duration_seconds_{bucket,count,sum}` |
| `http.server.active_requests` | `http_server_active_requests` |
| `pyobs.audit.events.recorded` | `pyobs_audit_events_recorded_total` |
| `pyobs.audit.events.delivered` | `pyobs_audit_events_delivered_total` |
| `pyobs.audit.queue.depth` | `pyobs_audit_queue_depth` |
| `pyobs.audit.delivery.latency` | `pyobs_audit_delivery_latency_seconds_{bucket,count,sum}` |

### Correlating the three signals

Every log line carries `trace_id` and `span_id` as structured metadata in Loki, and the dashboard's
logs panel and traces table both show real data from the same requests. The stack provisions the
links between them: the Loki datasource has a derived field that turns `trace_id` into an
**Open trace** link to Tempo, and the Tempo datasource has a trace-to-logs link to Loki. Both are
configured and loaded by Grafana, but the clicks themselves are not exercised by this demo. The
`request_id` in each line also appears in the `X-Request-ID` response header, so a user report that
quotes it leads straight to the logs.

## What is intentionally minimal

This stack is for development and demos, not production:

- Grafana runs with anonymous admin access and no login. Remove the `GF_AUTH_*` settings and set real
  credentials before exposing it.
- Nothing is persisted: Prometheus, Tempo and Loki write to container storage, so `down` loses the data.
  Mount volumes for anything you want to keep.
- Single-instance storage with local filesystem backends. For production use managed Grafana, Mimir,
  Tempo and Loki, or point the Collector at your existing backends.
- Image versions are pinned (Collector 0.117.0, Prometheus 3.1.0, Tempo 2.7.0, Loki 3.3.2, Grafana
  11.4.0). Newer releases may change configuration keys.

To use other backends, keep the SDK settings above and change only the Collector's `exporters` and
`service.pipelines` in `examples/grafana/otel-collector.yaml`. The dashboard JSON is
`examples/grafana/grafana/dashboards/pyobservability.json` and can be imported into any Grafana whose
datasource UIDs are `prometheus`, `tempo` and `loki`.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Panels are empty and the `app` log prints spans and metrics as JSON | The exporters are still `console`. Set `PYOBS_TRACING__EXPORTER=otlp` and `PYOBS_METRICS__EXPORTER=otlp` (or a non-development environment) |
| Metrics appear but there are no logs in Loki | Set `PYOBS_LOGGING__EXPORT_TO_OTEL=true` |
| First minute is empty | Metrics export every 60 s by default; the compose file sets `PYOBS_METRICS__EXPORT_INTERVAL_MS=5000` |
| `Recent traces` is empty right after start | Traces are batched for a few seconds before they reach Tempo; refresh |
| Collector logs `connection refused` to a backend | The backend is still starting; the Collector retries |
