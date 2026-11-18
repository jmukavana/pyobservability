"""Django integration.

Add `"pyobservability.integrations.django"` to `INSTALLED_APPS` (configures the SDK from
`settings.PYOBSERVABILITY`) and `"pyobservability.integrations.django.ObservabilityMiddleware"`
to the top of `MIDDLEWARE`.
"""

from collections.abc import Awaitable, Callable
from typing import Any, cast

from pyobservability.exceptions import MissingExtraError
from pyobservability.integrations.common import HttpRequest, ServerRequest, begin_server_request

try:
    from asgiref.sync import iscoroutinefunction, markcoroutinefunction
    from django.http import HttpRequest as DjangoRequest
    from django.http import HttpResponseBase
except ImportError as error:
    raise MissingExtraError("django", "the Django integration") from error

_OBSERVED = "_pyobservability_request"

GetResponse = Callable[[DjangoRequest], HttpResponseBase | Awaitable[HttpResponseBase]]


class ObservabilityMiddleware:
    sync_capable = True
    async_capable = True

    def __init__(self, get_response: GetResponse) -> None:
        self.get_response = get_response
        self._is_async = iscoroutinefunction(get_response)
        if self._is_async:
            markcoroutinefunction(self)

    def __call__(self, request: DjangoRequest) -> Any:
        if self._is_async:
            return self._acall(request)
        observed = _begin(request)
        try:
            response = cast(HttpResponseBase, self.get_response(request))
            _record_response(observed, response)
            return response
        except BaseException as error:
            _fail(observed, error)
            raise
        finally:
            _finish(observed, request)

    async def _acall(self, request: DjangoRequest) -> HttpResponseBase:
        observed = _begin(request)
        try:
            response = await cast(Awaitable[HttpResponseBase], self.get_response(request))
            _record_response(observed, response)
            return response
        except BaseException as error:
            _fail(observed, error)
            raise
        finally:
            _finish(observed, request)

    def process_exception(self, request: DjangoRequest, exception: Exception) -> None:
        _fail(getattr(request, _OBSERVED, None), exception)


def _begin(request: DjangoRequest) -> ServerRequest | None:
    observed = begin_server_request(
        HttpRequest(
            method=request.method or "GET",
            path=request.path,
            scheme=request.scheme or "http",
            headers=dict(request.headers.items()),
            client_address=request.META.get("REMOTE_ADDR"),
        )
    )
    setattr(request, _OBSERVED, observed)
    return observed


def _record_response(observed: ServerRequest | None, response: HttpResponseBase) -> None:
    if observed is None:
        return
    observed.set_status(response.status_code)
    for name, value in observed.response_headers().items():
        if name not in response:
            response[name] = value


def _fail(observed: ServerRequest | None, error: BaseException) -> None:
    if observed is not None:
        observed.fail(error)


def _finish(observed: ServerRequest | None, request: DjangoRequest) -> None:
    if observed is None:
        return
    match = request.resolver_match
    observed.set_route(f"/{match.route}" if match and match.route else None)
    observed.finish()


__all__ = ["ObservabilityMiddleware"]
