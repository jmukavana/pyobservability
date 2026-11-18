"""Flask integration: `PyObservability(app)` or `PyObservability().init_app(app)`."""

from typing import Any, cast

from pyobservability.exceptions import MissingExtraError
from pyobservability.integrations.common import HttpRequest, ServerRequest, begin_server_request

try:
    from flask import Flask, Response, g, got_request_exception, request
except ImportError as error:
    raise MissingExtraError("flask", "the Flask integration") from error

EXTENSION_KEY = "pyobservability"
_OBSERVED = "_pyobservability_request"


class PyObservability:
    def __init__(self, app: Flask | None = None) -> None:
        if app is not None:
            self.init_app(app)

    def init_app(self, app: Flask) -> None:
        if EXTENSION_KEY in app.extensions:
            return
        app.extensions[EXTENSION_KEY] = self
        app.before_request(_begin)
        app.after_request(_record_response)
        app.teardown_request(_finish)
        got_request_exception.connect(_record_exception, app)


def _begin() -> None:
    setattr(
        g,
        _OBSERVED,
        begin_server_request(
            HttpRequest(
                method=request.method,
                path=request.path,
                scheme=request.scheme,
                headers=dict(request.headers),
                client_address=request.remote_addr,
            )
        ),
    )


def _record_response(response: Response) -> Response:
    observed = _observed()
    if observed is not None:
        observed.set_route(request.url_rule.rule if request.url_rule else None)
        observed.set_status(response.status_code)
        for name, value in observed.response_headers().items():
            response.headers.setdefault(name, value)
    return response


def _record_exception(sender: Flask, exception: BaseException, **_: Any) -> None:
    if (observed := _observed()) is not None:
        observed.fail(exception)


def _finish(error: BaseException | None) -> None:
    observed = g.pop(_OBSERVED, None)
    if observed is None:
        return
    if error is not None:
        observed.fail(error)
    observed.finish()


def _observed() -> ServerRequest | None:
    return cast(ServerRequest | None, g.get(_OBSERVED))


__all__ = ["PyObservability"]
