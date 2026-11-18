"""FastAPI and Starlette integration: `app.add_middleware(ObservabilityMiddleware)`."""

from pyobservability.integrations.asgi import ObservabilityMiddleware

__all__ = ["ObservabilityMiddleware"]
