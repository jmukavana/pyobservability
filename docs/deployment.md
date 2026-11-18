# Deployment

## Containers

Configure through environment variables and keep the code unchanged. A complete image and compose
file are in `examples/fastapi/Dockerfile` and `examples/collector/docker-compose.yml`.

If audit uses `boundary="disk"`, mount the spool directory on a persistent volume. A spool directory
is owned by one process at a time, so run one writer per volume.

## Kubernetes

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: claims-service
spec:
  replicas: 3
  selector:
    matchLabels: {app: claims-service}
  template:
    metadata:
      labels: {app: claims-service}
    spec:
      terminationGracePeriodSeconds: 30   # must exceed shutdown_timeout_s
      containers:
        - name: app
          image: registry.example.com/claims-service:1.0.0
          env:
            - {name: PYOBS_SERVICE_NAME, value: claims-service}
            - {name: PYOBS_ENVIRONMENT, value: production}
            - {name: OTEL_EXPORTER_OTLP_ENDPOINT, value: "http://otel-collector.observability:4317"}
            - {name: PYOBS_AUDITING__DURABILITY__BOUNDARY, value: outbox}
            - {name: PYOBS_AUDITING__DURABILITY__OUTBOX__DSN_ENV, value: DATABASE_URL}
            - name: DATABASE_URL
              valueFrom: {secretKeyRef: {name: claims-db, key: url}}
```

In production the SDK refuses to start with audit held only in memory. Pods are replaceable and their
disks are not durable, so use `boundary="outbox"` as above (events commit with your database
transaction and any replica can relay them), or `boundary="exporter"` configured in code. Use
`boundary="disk"` only with a persistent volume per pod, such as a StatefulSet.

## gunicorn and other forking servers

Delivery threads do not survive `fork()`, so configure in each worker, not in the master:

```python
# gunicorn.conf.py
def post_fork(server, worker):
    import pyobservability

    pyobservability.configure(service_name="claims-service")  # durability etc. from PYOBS_* env
```

Do not use `--preload` with audit enabled. With `boundary="disk"` every worker needs its own spool
directory; use `boundary="outbox"` to share one store across workers instead.

Celery prefork workers follow the same rule: call `configure()` in `worker_process_init`.

## Upgrades and shutdown

Send `SIGTERM` and let the process exit normally so `shutdown()` runs and drains audit within
`shutdown_timeout_s`. Events still in flight after the timeout are dead-lettered rather than lost,
and may be delivered twice, so consumers de-duplicate on `event_id`.
