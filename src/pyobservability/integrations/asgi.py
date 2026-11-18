from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

from pyobservability.integrations.common import HttpRequest, ServerRequest, begin_server_request

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

HEADER_ENCODING = "latin-1"


class ObservabilityMiddleware:
    """Pure ASGI middleware: no framework dependency and safe for streaming responses."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        observed = (
            begin_server_request(_http_request(scope), framework_traces=_fastapi_traces(scope))
            if scope["type"] == "http"
            else None
        )
        if observed is None:
            await self.app(scope, receive, send)
            return

        async def send_observed(message: Message) -> None:
            if message["type"] == "http.response.start":
                observed.set_route(_route_template(scope))
                observed.set_status(message["status"])
                _add_missing_headers(message, observed)
            await send(message)

        try:
            await self.app(scope, receive, send_observed)
        except BaseException as error:
            observed.fail(error)
            raise
        finally:
            observed.set_route(_route_template(scope))
            observed.finish()


def _http_request(scope: Scope) -> HttpRequest:
    headers = {
        name.decode(HEADER_ENCODING): value.decode(HEADER_ENCODING)
        for name, value in scope.get("headers", ())
    }
    client = scope.get("client")
    return HttpRequest(
        method=scope["method"],
        path=scope["path"],
        scheme=scope.get("scheme", "http"),
        headers=headers,
        client_address=client[0] if client else None,
    )


def _fastapi_traces(scope: Scope) -> bool:
    """FastAPI's native telemetry wraps the middleware stack and leaves its state in the scope."""
    native = scope.get("fastapi.telemetry")
    return getattr(native, "span", None) is not None


def _route_template(scope: Scope) -> str | None:
    """Starlette and FastAPI record the matched route in the scope they share with middleware."""
    route = scope.get("route")
    path = getattr(route, "path", None)
    return path if isinstance(path, str) else None


def _add_missing_headers(message: Message, observed: ServerRequest) -> None:
    existing = {name.decode(HEADER_ENCODING).lower() for name, _ in message.get("headers", ())}
    added = [
        (name.encode(HEADER_ENCODING), value.encode(HEADER_ENCODING))
        for name, value in observed.response_headers().items()
        if name not in existing
    ]
    message["headers"] = [*message.get("headers", ()), *added]
