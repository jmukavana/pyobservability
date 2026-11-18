import asyncio
import inspect
from collections.abc import Iterator
from typing import Any

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace import TracerProvider as SdkTracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

import pyobservability
from pyobservability import get_tracer, traced
from pyobservability.config import load_config
from pyobservability.exporters import span_exporters
from pyobservability.exporters.otlp import signal_endpoint
from pyobservability.security import Redactor
from pyobservability.tracing import TracingComponent, context_from_headers, inject_trace_headers

MEMORY_EXPORTER = "memory"


class CurrentExporter:
    exporter = InMemorySpanExporter()


def _memory_exporter(**_: Any) -> InMemorySpanExporter:
    return CurrentExporter.exporter


if MEMORY_EXPORTER not in span_exporters.names():
    span_exporters.register(MEMORY_EXPORTER, _memory_exporter)


@pytest.fixture
def exporter() -> Iterator[InMemorySpanExporter]:
    CurrentExporter.exporter = InMemorySpanExporter()
    yield CurrentExporter.exporter


def configure_tracing(**tracing: Any) -> None:
    pyobservability.configure(
        service_name="claims-service",
        service_version="2.0.0",
        auditing={"enabled": False},
        logging={"enabled": False},
        tracing={"exporter": MEMORY_EXPORTER, **tracing},
    )


def finished_spans(exporter: InMemorySpanExporter) -> list[ReadableSpan]:
    pyobservability.shutdown()
    return list(exporter.get_finished_spans())


def only_span(exporter: InMemorySpanExporter) -> ReadableSpan:
    spans = finished_spans(exporter)
    assert len(spans) == 1, spans
    return spans[0]


class TestExport:
    def test_spans_carry_service_resource(self, exporter: InMemorySpanExporter) -> None:
        configure_tracing()

        with get_tracer(__name__).start_as_current_span("work"):
            pass
        resource = only_span(exporter).resource.attributes

        assert resource["service.name"] == "claims-service"
        assert resource["service.version"] == "2.0.0"
        assert resource["deployment.environment.name"] == "development"

    def test_none_exporter_still_creates_valid_spans(self) -> None:
        configure_tracing(exporter="none")

        with get_tracer(__name__).start_as_current_span("work") as span:
            assert span.get_span_context().is_valid

    def test_disabled_tracing_creates_no_recording_spans(self) -> None:
        configure_tracing(enabled=False)

        with get_tracer(__name__).start_as_current_span("work") as span:
            assert not span.is_recording()

    def test_unknown_exporter_fails_configuration(self) -> None:
        with pytest.raises(pyobservability.ConfigurationError, match="unknown span exporter"):
            configure_tracing(exporter="zipkin-classic")

    def test_tracer_obtained_early_follows_reconfiguration(
        self, exporter: InMemorySpanExporter
    ) -> None:
        early_tracer = get_tracer("early")
        configure_tracing()
        pyobservability.shutdown()
        CurrentExporter.exporter = second_exporter = InMemorySpanExporter()
        configure_tracing()

        with early_tracer.start_as_current_span("after reconfigure"):
            pass

        assert [span.name for span in finished_spans(second_exporter)] == ["after reconfigure"]

    def test_spans_after_shutdown_are_not_recorded(self, exporter: InMemorySpanExporter) -> None:
        configure_tracing()
        pyobservability.shutdown()

        with get_tracer(__name__).start_as_current_span("late") as span:
            assert not span.is_recording()


class TestSampling:
    def test_always_off_records_nothing(self, exporter: InMemorySpanExporter) -> None:
        configure_tracing(sampler="always_off")

        with get_tracer(__name__).start_as_current_span("dropped"):
            pass

        assert finished_spans(exporter) == []

    def test_zero_ratio_records_nothing(self, exporter: InMemorySpanExporter) -> None:
        configure_tracing(sample_rate=0.0)

        with get_tracer(__name__).start_as_current_span("dropped"):
            pass

        assert finished_spans(exporter) == []


@traced
def approve(claim_id: str) -> str:
    return f"approved {claim_id}"


@traced(name="claims.reject", attributes={"claims.reason": "duplicate"})
async def reject(claim_id: str) -> str:
    await asyncio.sleep(0)
    return f"rejected {claim_id}"


@traced
def explode() -> None:
    raise ValueError("cannot approve")


class TestTracedDecorator:
    def test_sync_function_gets_a_span(self, exporter: InMemorySpanExporter) -> None:
        configure_tracing()

        assert approve("CLM-1") == "approved CLM-1"
        span = only_span(exporter)

        assert span.name == "approve"
        assert span.attributes is not None
        assert span.attributes["code.function.name"] == f"{__name__}.approve"

    async def test_async_function_gets_a_named_span(self, exporter: InMemorySpanExporter) -> None:
        configure_tracing()

        assert await reject("CLM-2") == "rejected CLM-2"
        span = only_span(exporter)

        assert span.name == "claims.reject"
        assert span.attributes is not None
        assert span.attributes["claims.reason"] == "duplicate"

    def test_exceptions_are_recorded_and_reraised(self, exporter: InMemorySpanExporter) -> None:
        configure_tracing()

        with pytest.raises(ValueError, match="cannot approve"):
            explode()
        span = only_span(exporter)

        assert span.status.status_code is StatusCode.ERROR
        assert [event.name for event in span.events] == ["exception"]

    def test_metadata_of_wrapped_function_is_kept(self) -> None:
        assert approve.__name__ == "approve"
        assert inspect.iscoroutinefunction(reject)


class TestRedaction:
    def test_secret_attributes_are_masked(self, exporter: InMemorySpanExporter) -> None:
        configure_tracing()

        with get_tracer(__name__).start_as_current_span("call") as span:
            span.set_attribute("http.request.header.authorization", "Bearer abc.def")
            span.set_attribute("url.full", "https://api.example/claims?access_token=s3cr3t&page=2")
            span.set_attribute("claim.id", "CLM-1")
        attributes = only_span(exporter).attributes

        assert attributes is not None
        assert attributes["http.request.header.authorization"] == "[REDACTED]"
        assert attributes["url.full"] == "https://api.example/claims?access_token=[REDACTED]&page=2"
        assert attributes["claim.id"] == "CLM-1"

    def test_exception_details_are_masked(self, exporter: InMemorySpanExporter) -> None:
        configure_tracing()

        with (
            pytest.raises(ConnectionError),
            get_tracer(__name__).start_as_current_span("query"),
        ):
            raise ConnectionError("postgres://admin:hunter2@db/claims refused")
        span = only_span(exporter)
        exported_text = repr(span.status.description) + repr(span.events[0].attributes)

        assert "hunter2" not in exported_text
        assert span.status.status_code is StatusCode.ERROR

    def test_span_event_attributes_are_masked(self, exporter: InMemorySpanExporter) -> None:
        configure_tracing()

        with get_tracer(__name__).start_as_current_span("login") as span:
            span.add_event("credentials checked", {"password": "hunter2"})
        attributes = only_span(exporter).events[0].attributes

        assert attributes is not None
        assert attributes["password"] == "[REDACTED]"


class TestPropagation:
    def test_w3c_context_round_trips_between_services(self, exporter: InMemorySpanExporter) -> None:
        configure_tracing()
        tracer = get_tracer(__name__)
        outgoing_headers: dict[str, str] = {}

        with tracer.start_as_current_span("client call") as client_span:
            inject_trace_headers(outgoing_headers)
        with tracer.start_as_current_span(
            "server handle", context=context_from_headers(outgoing_headers)
        ) as server_span:
            pass

        assert outgoing_headers["traceparent"].startswith("00-")
        assert server_span.get_span_context().trace_id == client_span.get_span_context().trace_id
        server = next(span for span in finished_spans(exporter) if span.name == "server handle")
        assert server.parent is not None
        assert server.parent.span_id == client_span.get_span_context().span_id


class TestForeignProviders:
    def foreign_provider(self) -> tuple[SdkTracerProvider, InMemorySpanExporter]:
        provider = SdkTracerProvider()
        their_exporter = InMemorySpanExporter()
        provider.add_span_processor(SimpleSpanProcessor(their_exporter))
        return provider, their_exporter

    def test_attach_exports_their_spans_through_our_redacting_pipeline(
        self, exporter: InMemorySpanExporter
    ) -> None:
        provider, their_exporter = self.foreign_provider()
        config = load_config(
            service_name="svc", auditing={"enabled": False}, tracing={"exporter": MEMORY_EXPORTER}
        )
        component = TracingComponent(config, Redactor(config.security), "attach", provider)
        component.start()

        with provider.get_tracer(__name__).start_as_current_span("theirs") as span:
            span.set_attribute("api_key", "k-1")
        component.shutdown(timeout_s=1)

        assert [span.name for span in their_exporter.get_finished_spans()] == ["theirs"]
        ours = exporter.get_finished_spans()
        assert ours[0].attributes is not None
        assert ours[0].attributes["api_key"] == "[REDACTED]"

    def test_detached_processor_stops_exporting(self, exporter: InMemorySpanExporter) -> None:
        provider, _ = self.foreign_provider()
        config = load_config(
            service_name="svc", auditing={"enabled": False}, tracing={"exporter": MEMORY_EXPORTER}
        )
        component = TracingComponent(config, Redactor(config.security), "attach", provider)
        component.start()
        component.shutdown(timeout_s=1)

        with provider.get_tracer(__name__).start_as_current_span("after detach"):
            pass

        assert exporter.get_finished_spans() == ()

    def test_external_mode_leaves_global_provider_untouched(self) -> None:
        config = load_config(service_name="svc", auditing={"enabled": False})
        before = trace.get_tracer_provider()
        component = TracingComponent(config, Redactor(config.security), "external", before)

        component.start()
        component.shutdown(timeout_s=1)

        assert trace.get_tracer_provider() is before


@pytest.mark.parametrize(
    ("endpoint", "expected"),
    [
        ("http://collector:4318", "http://collector:4318/v1/traces"),
        ("http://collector:4318/", "http://collector:4318/v1/traces"),
        ("http://collector:4318/custom/path", "http://collector:4318/custom/path"),
        (None, None),
    ],
)
def test_http_endpoint_gets_signal_path(endpoint: str | None, expected: str | None) -> None:
    assert signal_endpoint(endpoint, "traces") == expected


def test_otlp_exporter_is_built_from_config() -> None:
    config = load_config(
        service_name="svc",
        auditing={"enabled": False},
        tracing={"exporter": "otlp", "endpoint": "http://collector:4318", "protocol": "http"},
    )

    exporter = span_exporters.create("otlp", config=config.tracing)

    assert type(exporter).__name__ == "OTLPSpanExporter"
    exporter.shutdown()


def test_missing_otlp_extra_names_the_install_command(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_otlp_exporters(name: str, package: str | None = None) -> Any:
        raise ImportError(name)

    monkeypatch.setattr("pyobservability.exporters.otlp.importlib.import_module", no_otlp_exporters)
    config = load_config(
        service_name="svc",
        auditing={"enabled": False},
        tracing={"exporter": "otlp", "endpoint": "http://collector:4317"},
    )

    with pytest.raises(pyobservability.MissingExtraError, match=r"pyobservability\[otlp\]"):
        span_exporters.create("otlp", config=config.tracing)


@pytest.mark.parametrize(
    ("endpoint", "insecure", "expect_plaintext"),
    [
        ("http://collector:4317", None, True),
        ("https://collector:4317", None, False),
        ("https://collector:4317", True, False),
        ("http://collector:4317", False, False),
    ],
)
def test_grpc_transport_security_follows_the_endpoint_unless_overridden(
    endpoint: str, insecure: bool | None, expect_plaintext: bool
) -> None:
    config = load_config(
        service_name="svc",
        auditing={"enabled": False},
        tracing={"exporter": "otlp", "endpoint": endpoint, "insecure": insecure},
    )

    exporter = span_exporters.create("otlp", config=config.tracing)

    assert exporter._insecure is expect_plaintext  # type: ignore[attr-defined]
    exporter.shutdown()
