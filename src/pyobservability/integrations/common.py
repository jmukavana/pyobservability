import time
from collections.abc import Mapping
from contextlib import ExitStack
from dataclasses import dataclass
from typing import Any, Self

from opentelemetry import trace
from opentelemetry.semconv._incubating.metrics.http_metrics import HTTP_SERVER_ACTIVE_REQUESTS
from opentelemetry.semconv.attributes.client_attributes import CLIENT_ADDRESS
from opentelemetry.semconv.attributes.error_attributes import ERROR_TYPE
from opentelemetry.semconv.attributes.http_attributes import (
    HTTP_REQUEST_METHOD,
    HTTP_RESPONSE_STATUS_CODE,
    HTTP_ROUTE,
    HttpRequestMethodValues,
)
from opentelemetry.semconv.attributes.url_attributes import URL_PATH, URL_SCHEME
from opentelemetry.semconv.attributes.user_agent_attributes import USER_AGENT_ORIGINAL
from opentelemetry.semconv.metrics.http_metrics import HTTP_SERVER_REQUEST_DURATION
from opentelemetry.trace import Span, SpanKind, Status, StatusCode

from pyobservability.config import ObservabilityConfig
from pyobservability.context import RequestContextBinder, get
from pyobservability.context.correlation import REQUEST_ID
from pyobservability.logging import get_logger
from pyobservability.metrics import get_meter
from pyobservability.runtime import active_runtime
from pyobservability.security.paths import templated_path
from pyobservability.tracing import context_from_headers, get_tracer

REQUEST_ID_HEADER = "x-request-id"
MAX_USER_AGENT_LENGTH = 256
FIRST_SERVER_ERROR = 500
_KNOWN_METHODS = frozenset(method.value for method in HttpRequestMethodValues)

_tracer = get_tracer("pyobservability.http")
_meter = get_meter("pyobservability.http")
_duration = _meter.create_histogram(
    HTTP_SERVER_REQUEST_DURATION, unit="s", description="Duration of HTTP server requests"
)
_active_requests = _meter.create_up_down_counter(
    HTTP_SERVER_ACTIVE_REQUESTS, unit="{request}", description="HTTP server requests in progress"
)
ACCESS_LOGGER = "pyobservability.access"


@dataclass(frozen=True, slots=True)
class HttpRequest:
    method: str
    path: str
    scheme: str
    headers: Mapping[str, str]
    client_address: str | None = None


def begin_server_request(
    request: HttpRequest, *, framework_traces: bool = False
) -> "ServerRequest | None":
    """Starts observing a request, or returns None when unconfigured or the path is excluded.

    `framework_traces` means the framework already emits the server span and HTTP metrics;
    only context, request-ID echo and the access log are added then, avoiding duplicates.
    """
    runtime = active_runtime.current
    if runtime is None or request.path in runtime.config.http.excluded_paths:
        return None
    return ServerRequest(request, runtime.config, owns_span=not framework_traces).start()


class ServerRequest:
    """Context, span, metrics and access log for one inbound HTTP request."""

    def __init__(
        self, request: HttpRequest, config: ObservabilityConfig, *, owns_span: bool
    ) -> None:
        self._request = request
        self._config = config
        self._owns_span = owns_span
        self._headers = {name.lower(): value for name, value in request.headers.items()}
        self._method = _normalized_method(request.method)
        self._stack = ExitStack()
        self._span: Span | None = None
        self._route: str | None = None
        self._status: int | None = None
        self._error: BaseException | None = None
        self._started_at = 0.0
        self._finished = False

    def start(self) -> Self:
        self._stack.enter_context(RequestContextBinder(self._config.context).bind(self._headers))
        self._started_at = time.perf_counter()
        if not self._owns_span:
            return self
        self._span = self._stack.enter_context(
            _tracer.start_as_current_span(
                self._method,
                context=context_from_headers(self._headers),
                kind=SpanKind.SERVER,
                attributes=self._request_attributes(),
                record_exception=False,
                set_status_on_exception=False,
            )
        )
        _active_requests.add(1, self._base_attributes())
        return self

    def set_route(self, route: str | None) -> None:
        self._route = route or self._route

    def set_status(self, status: int) -> None:
        self._status = status

    def fail(self, error: BaseException) -> None:
        self._error = error

    def response_headers(self) -> dict[str, str]:
        request_id = get(REQUEST_ID)
        if not self._config.http.echo_request_id or request_id is None:
            return {}
        return {REQUEST_ID_HEADER: str(request_id)}

    def finish(self) -> None:
        if self._finished:
            return
        self._finished = True
        duration_s = time.perf_counter() - self._started_at
        if self._owns_span:
            attributes = self._response_attributes()
            self._complete_span(attributes)
            _duration.record(duration_s, attributes)
            _active_requests.add(-1, self._base_attributes())
        elif self._error is not None:
            self._record_on_framework_span(self._error)
        self._log_access(duration_s)
        self._stack.close()

    @property
    def _effective_status(self) -> int | None:
        if self._error is not None and (self._status is None or self._status < FIRST_SERVER_ERROR):
            return FIRST_SERVER_ERROR
        return self._status

    @property
    def _failed(self) -> bool:
        status = self._effective_status
        return status is not None and status >= FIRST_SERVER_ERROR

    def _base_attributes(self) -> dict[str, Any]:
        return {HTTP_REQUEST_METHOD: self._method, URL_SCHEME: self._request.scheme}

    def _request_attributes(self) -> dict[str, Any]:
        attributes = {**self._base_attributes(), URL_PATH: self._request.path}
        if user_agent := self._headers.get("user-agent"):
            attributes[USER_AGENT_ORIGINAL] = user_agent[:MAX_USER_AGENT_LENGTH]
        if self._request.client_address:
            attributes[CLIENT_ADDRESS] = self._request.client_address
        for header in self._config.security.capture_headers:
            if (value := self._headers.get(header.lower())) is not None:
                attributes[f"http.request.header.{header.lower()}"] = [value]
        return attributes

    def _response_attributes(self) -> dict[str, Any]:
        attributes = self._base_attributes()
        if self._route:
            attributes[HTTP_ROUTE] = self._route
        if (status := self._effective_status) is not None:
            attributes[HTTP_RESPONSE_STATUS_CODE] = status
        if self._error is not None:
            attributes[ERROR_TYPE] = _qualified_name(type(self._error))
        elif self._failed:
            attributes[ERROR_TYPE] = str(self._effective_status)
        return attributes

    def _complete_span(self, attributes: dict[str, Any]) -> None:
        if self._span is None:
            return
        self._span.update_name(f"{self._method} {self._route}" if self._route else self._method)
        self._span.set_attributes(attributes)
        if self._error is not None:
            self._span.record_exception(self._error)
        if self._failed:
            self._span.set_status(Status(StatusCode.ERROR))

    @staticmethod
    def _record_on_framework_span(error: BaseException) -> None:
        """Some frameworks report exceptions only through OTel logs; keep them in the trace."""
        span = trace.get_current_span()
        if span.is_recording():
            span.record_exception(error)

    def _log_access(self, duration_s: float) -> None:
        if not self._config.http.access_log:
            return
        access_log = get_logger(ACCESS_LOGGER)
        log = access_log.error if self._failed else access_log.info
        log(
            "request completed",
            method=self._method,
            route=self._route,
            path=self._display_path(),
            status=self._effective_status,
            duration_ms=round(duration_s * 1000, 3),
        )

    def _display_path(self) -> str:
        if self._config.security.path_param_redaction:
            return templated_path(self._request.path)
        return self._request.path


def _qualified_name(error_type: type[BaseException]) -> str:
    if error_type.__module__ == "builtins":
        return error_type.__qualname__
    return f"{error_type.__module__}.{error_type.__qualname__}"


def _normalized_method(method: str) -> str:
    """Unknown methods would otherwise give clients control over span names and metric labels."""
    upper = method.upper()
    return upper if upper in _KNOWN_METHODS else HttpRequestMethodValues.OTHER.value
