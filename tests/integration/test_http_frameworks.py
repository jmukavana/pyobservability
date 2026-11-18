import asyncio
import json
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any

import django
import httpx
import pytest
from django.conf import settings as django_settings
from django.http import HttpRequest as DjangoRequest
from django.http import HttpResponse, JsonResponse
from django.test import AsyncClient, Client, override_settings
from django.urls import path as django_path
from fastapi import FastAPI
from flask import Flask
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.trace import SpanKind, StatusCode
from starlette.applications import Starlette
from starlette.requests import Request as StarletteRequest
from starlette.responses import JSONResponse as StarletteJson
from starlette.routing import Route

from pyobservability import context
from pyobservability.config import DEFAULT_EXCLUDED_PATHS
from pyobservability.integrations.fastapi import ObservabilityMiddleware as AsgiMiddleware
from pyobservability.integrations.flask import PyObservability

OUR_SCOPE = "pyobservability.http"
TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
PARENT_SPAN_ID = "00f067aa0ba902b7"
TRACEPARENT = f"00-{TRACE_ID}-{PARENT_SPAN_ID}-01"


def claim_payload(claim_id: str) -> dict[str, Any]:
    return {
        "claim_id": claim_id,
        "request_id": context.get("request_id"),
        "tenant_id": context.get("tenant_id"),
    }


class Boom(RuntimeError):
    pass


def fastapi_app() -> FastAPI:
    """FastAPI traces natively; its own `exclude` keeps health checks out of its telemetry."""
    app = FastAPI(telemetry={"exclude": lambda scope: scope["path"] in DEFAULT_EXCLUDED_PATHS})
    app.add_middleware(AsgiMiddleware)

    @app.get("/claims/{claim_id}")
    async def get_claim(claim_id: str) -> dict[str, Any]:
        return claim_payload(claim_id)

    @app.get("/boom")
    async def boom() -> None:
        raise Boom("handler crashed")

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


def flask_app() -> Flask:
    app = Flask(__name__)
    PyObservability(app)

    @app.get("/claims/<claim_id>")
    def get_claim(claim_id: str) -> dict[str, Any]:
        return claim_payload(claim_id)

    @app.get("/boom")
    def boom() -> dict[str, Any]:
        raise Boom("handler crashed")

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


def django_get_claim(request: DjangoRequest, claim_id: str) -> JsonResponse:
    return JsonResponse(claim_payload(claim_id))


def django_boom(request: DjangoRequest) -> HttpResponse:
    raise Boom("handler crashed")


def django_health(request: DjangoRequest) -> JsonResponse:
    return JsonResponse({"status": "ok"})


async def django_async_claim(request: DjangoRequest, claim_id: str) -> JsonResponse:
    return JsonResponse(claim_payload(claim_id))


urlpatterns = [
    django_path("claims/<str:claim_id>", django_get_claim),
    django_path("async/claims/<str:claim_id>", django_async_claim),
    django_path("boom", django_boom),
    django_path("health", django_health),
]

if not django_settings.configured:
    django_settings.configure(
        DEBUG=False,
        SECRET_KEY="tests-only",  # noqa: S106
        ALLOWED_HOSTS=["testserver"],
        ROOT_URLCONF=__name__,
        INSTALLED_APPS=[],
        MIDDLEWARE=["pyobservability.integrations.django.ObservabilityMiddleware"],
        LOGGING_CONFIG=None,
    )
    django.setup()


@dataclass(frozen=True)
class Reply:
    status: int
    headers: Mapping[str, str]
    body: Any


@dataclass(frozen=True)
class Framework:
    name: str
    claim_route: str
    get: Callable[[str, dict[str, str]], Reply]


def asgi_request(app: Any, method: str, path: str, headers: dict[str, str]) -> httpx.Response:
    async def send() -> httpx.Response:
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.request(method, path, headers=headers)

    return asyncio.run(send())


def _fastapi_client() -> Callable[[str, dict[str, str]], Reply]:
    app = fastapi_app()

    def get(path: str, headers: dict[str, str]) -> Reply:
        response = asgi_request(app, "GET", path, headers)
        return Reply(response.status_code, dict(response.headers), _json_or_none(response.text))

    return get


def _flask_client() -> Callable[[str, dict[str, str]], Reply]:
    client = flask_app().test_client()

    def get(path: str, headers: dict[str, str]) -> Reply:
        response = client.get(path, headers=headers)
        return Reply(response.status_code, dict(response.headers), _json_or_none(response.text))

    return get


def _django_client() -> Callable[[str, dict[str, str]], Reply]:
    client = Client(raise_request_exception=False)

    def get(path: str, headers: dict[str, str]) -> Reply:
        response = client.get(path, headers=headers)
        return Reply(response.status_code, dict(response.headers), _json_or_none(response.content))

    return get


def _json_or_none(text: str | bytes) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return None


FRAMEWORKS = {
    "fastapi": lambda: Framework("fastapi", "/claims/{claim_id}", _fastapi_client()),
    "flask": lambda: Framework("flask", "/claims/<claim_id>", _flask_client()),
    "django": lambda: Framework("django", "/claims/<str:claim_id>", _django_client()),
}


@pytest.fixture(params=sorted(FRAMEWORKS))
def framework(request: pytest.FixtureRequest) -> Iterator[Framework]:
    yield FRAMEWORKS[request.param]()


def server_span(spans: list[ReadableSpan]) -> ReadableSpan:
    [span] = [span for span in spans if span.kind is SpanKind.SERVER]
    return span


class TestServerSpans:
    def test_span_is_named_after_the_route_template(
        self, framework: Framework, telemetry: Any
    ) -> None:
        telemetry.configure()

        framework.get("/claims/123", {})
        span = server_span(telemetry.spans())

        assert span.name == f"GET {framework.claim_route}"
        assert span.attributes is not None
        assert span.attributes["http.route"] == framework.claim_route
        assert span.attributes["http.response.status_code"] == 200
        assert span.attributes["http.request.method"] == "GET"
        assert span.attributes["url.path"] == "/claims/{id}"

    def test_incoming_trace_context_is_continued(
        self, framework: Framework, telemetry: Any
    ) -> None:
        telemetry.configure()

        framework.get("/claims/1", {"traceparent": TRACEPARENT})
        span = server_span(telemetry.spans())

        assert format(span.context.trace_id, "032x") == TRACE_ID
        assert span.parent is not None
        assert format(span.parent.span_id, "016x") == PARENT_SPAN_ID

    def test_unhandled_exceptions_mark_the_span_as_failed(
        self, framework: Framework, telemetry: Any
    ) -> None:
        telemetry.configure()

        reply = framework.get("/boom", {})
        spans = telemetry.spans()
        span = server_span(spans)

        assert reply.status == 500
        assert span.status.status_code is StatusCode.ERROR
        assert span.attributes is not None
        assert span.attributes["error.type"] == f"{Boom.__module__}.{Boom.__qualname__}"
        assert span.attributes["http.response.status_code"] == 500
        recorded = [event.name for trace_span in spans for event in trace_span.events]
        assert recorded == ["exception"]

    def test_unknown_routes_keep_a_bounded_name(self, framework: Framework, telemetry: Any) -> None:
        telemetry.configure()

        reply = framework.get("/no/such/9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822c", {})
        span = server_span(telemetry.spans())

        assert reply.status == 404
        assert span.name == "GET"
        assert span.attributes is not None
        assert span.attributes["url.path"] == "/no/such/{id}"
        assert span.status.status_code is not StatusCode.ERROR

    def test_excluded_paths_are_not_observed(self, framework: Framework, telemetry: Any) -> None:
        telemetry.configure()

        assert framework.get("/health", {}).status == 200
        assert telemetry.spans() == []


class TestRequestContext:
    def test_handler_sees_request_and_tenant_ids(
        self, framework: Framework, telemetry: Any
    ) -> None:
        telemetry.configure(context={"tenant_header": "X-Tenant-ID"})

        reply = framework.get(
            "/claims/7", {"X-Request-ID": "req-abc-12345", "X-Tenant-ID": "tenant-42"}
        )

        assert reply.body["request_id"] == "req-abc-12345"
        assert reply.body["tenant_id"] == "tenant-42"
        assert reply.headers["x-request-id"] == "req-abc-12345"

    def test_request_id_is_generated_and_echoed(self, framework: Framework, telemetry: Any) -> None:
        telemetry.configure()

        reply = framework.get("/claims/7", {})

        assert reply.headers["x-request-id"] == reply.body["request_id"]
        assert len(reply.body["request_id"]) == 32

    def test_context_does_not_leak_after_the_request(
        self, framework: Framework, telemetry: Any
    ) -> None:
        telemetry.configure()

        framework.get("/claims/7", {"X-Request-ID": "req-abc-12345"})

        assert context.snapshot() == {}

    def test_unconfigured_sdk_leaves_requests_untouched(self, framework: Framework) -> None:
        reply = framework.get("/claims/7", {})

        assert reply.status == 200
        assert "x-request-id" not in {name.lower() for name in reply.headers}


class TestHttpMetrics:
    def test_duration_is_recorded_with_bounded_labels(
        self, framework: Framework, telemetry: Any
    ) -> None:
        telemetry.configure()

        framework.get("/claims/1", {})
        framework.get("/claims/2", {})
        [point] = telemetry.points("http.server.request.duration")

        assert point.count == 2
        assert {
            "http.request.method": "GET",
            "url.scheme": "http",
            "http.route": framework.claim_route,
            "http.response.status_code": 200,
        }.items() <= point.attributes.items()
        assert not {"url.path", "client.address", "server.address"} & point.attributes.keys()

    def test_active_requests_return_to_zero(self, framework: Framework, telemetry: Any) -> None:
        telemetry.configure()

        framework.get("/claims/1", {})
        framework.get("/boom", {})
        [point] = telemetry.points("http.server.active_requests")

        assert point.value == 0


def test_access_log_is_written_once_per_request(
    telemetry: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    telemetry.configure(logging={"output": "stderr"})
    framework = FRAMEWORKS["fastapi"]()

    framework.get("/claims/1?api_key=s3cr3t", {})
    lines = [json.loads(line) for line in capsys.readouterr().err.splitlines()]
    access = [line for line in lines if line.get("logger") == "pyobservability.access"]

    assert len(access) == 1
    assert access[0]["route"] == "/claims/{claim_id}"
    assert access[0]["status"] == 200
    assert "s3cr3t" not in json.dumps(lines)


def test_unknown_http_methods_do_not_become_labels(telemetry: Any) -> None:
    telemetry.configure()
    asgi_request(fastapi_app(), "BREW", "/claims/1", {})
    span = server_span(telemetry.spans())

    assert span.attributes is not None
    assert span.attributes["http.request.method"] == "_OTHER"


async def test_django_async_views_are_observed(telemetry: Any) -> None:
    telemetry.configure()

    response = await AsyncClient().get("/async/claims/9", headers={"X-Request-ID": "req-async-123"})
    span = server_span(telemetry.spans())

    assert response.status_code == 200
    assert response.headers["x-request-id"] == "req-async-123"
    assert span.name == "GET /async/claims/<str:claim_id>"


def test_flask_extension_installs_once() -> None:
    app = Flask(__name__)

    PyObservability(app)
    PyObservability(app)

    assert len(app.before_request_funcs[None]) == 1


def our_server_spans(spans: list[ReadableSpan]) -> list[ReadableSpan]:
    return [
        span
        for span in spans
        if span.kind is SpanKind.SERVER
        and span.instrumentation_scope is not None
        and span.instrumentation_scope.name == OUR_SCOPE
    ]


def test_fastapi_without_native_tracing_gets_our_span(telemetry: Any) -> None:
    telemetry.configure()
    app = FastAPI(telemetry={"tracing": False, "metrics": False})
    app.add_middleware(AsgiMiddleware)

    @app.get("/claims/{claim_id}")
    async def get_claim(claim_id: str) -> dict[str, str]:
        return {"claim_id": claim_id}

    asgi_request(app, "GET", "/claims/1", {})
    [span] = our_server_spans(telemetry.spans())

    assert span.name == "GET /claims/{claim_id}"


def test_plain_starlette_apps_are_observed(telemetry: Any) -> None:
    telemetry.configure()

    async def claim(request: StarletteRequest) -> StarletteJson:
        return StarletteJson({"request_id": context.get("request_id")})

    app = Starlette(routes=[Route("/claims/{claim_id}", claim)])
    app.add_middleware(AsgiMiddleware)

    response = asgi_request(app, "GET", "/claims/1", {"X-Request-ID": "req-starlette-1"})
    [span] = our_server_spans(telemetry.spans())

    assert response.json() == {"request_id": "req-starlette-1"}
    assert span.name == "GET /claims/{claim_id}"


def test_allow_listed_headers_and_user_agent_are_captured(telemetry: Any) -> None:
    telemetry.configure(security={"capture_headers": ["X-Client-Version"], "allow_weakening": True})
    get = _flask_client()

    get("/claims/1", {"X-Client-Version": "4.2", "User-Agent": "claims-app/1.0", "Cookie": "a=b"})
    attributes = server_span(telemetry.spans()).attributes or {}

    assert attributes["http.request.header.x-client-version"] == ["4.2"]
    assert attributes["user_agent.original"] == "claims-app/1.0"
    assert not any("cookie" in key for key in attributes)


def test_django_app_config_configures_from_settings() -> None:
    import pyobservability.integrations.django as integration  # noqa: PLC0415
    from pyobservability.integrations.django.apps import PyObservabilityAppConfig  # noqa: PLC0415
    from pyobservability.runtime import active_runtime  # noqa: PLC0415

    app_config = PyObservabilityAppConfig("pyobservability.integrations.django", integration)
    with override_settings(
        PYOBSERVABILITY={"service_name": "django-claims", "auditing": {"enabled": False}}
    ):
        app_config.ready()

    assert active_runtime.current is not None
    assert active_runtime.current.config.service_name == "django-claims"
