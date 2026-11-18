# Web frameworks

Every integration does the same for each request: binds request and tenant context, continues an
incoming W3C `traceparent`, opens a SERVER span named by route template (`GET /claims/{claim_id}`),
records `http.server.request.duration` and `http.server.active_requests`, writes one access log, and
returns `X-Request-ID`. Context is cleared afterwards, on errors and cancellation too. Paths in
`http.excluded_paths` (health checks by default) are skipped, and nothing happens if the SDK is not
configured. Request bodies are never read.

## FastAPI, Starlette and any ASGI app

```python
from pyobservability.integrations.fastapi import ObservabilityMiddleware

app.add_middleware(ObservabilityMiddleware)
```

A pure ASGI middleware (no framework dependency, safe for streaming). FastAPI traces requests
natively; when it does, PyObservability leaves the span and HTTP metrics to FastAPI and adds context,
request-ID echo and the access log, and records unhandled exceptions on FastAPI's span. To keep health
checks out of FastAPI's own telemetry use `FastAPI(telemetry={"exclude": ...})`.

## Flask

```python
from pyobservability.integrations.flask import PyObservability

PyObservability(app)  # or PyObservability().init_app(app); installing twice is harmless
```

## Django

```python
INSTALLED_APPS = ["pyobservability.integrations.django", ...]  # configures from the setting below
MIDDLEWARE = ["pyobservability.integrations.django.ObservabilityMiddleware", ...]  # keep it first
PYOBSERVABILITY = {"service_name": "claims-service", "environment": "production"}
```

Works with both sync and async views.

## Handlers

```python
from pyobservability import context

context.set(user_id=current_user.id)  # picked up by logs and audit events in this request
```

Shut down cleanly: `await pyobservability.ashutdown()` in a FastAPI lifespan, or let the `atexit`
hook flush on exit. See [Operations](../operations.md).
