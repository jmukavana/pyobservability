from collections.abc import Iterator
from typing import Any

import pytest
from celery import Celery
from celery.contrib.testing.worker import start_worker
from opentelemetry.trace import SpanKind, StatusCode

from pyobservability import context, get_tracer
from pyobservability.integrations.celery import instrument_celery

pytestmark = pytest.mark.integration

app = Celery("claims", broker="memory://", backend="cache+memory://")
app.conf.update(task_serializer="json", result_serializer="json", accept_content=["json"])


@app.task(name="claims.approve")  # type: ignore[untyped-decorator]  # celery is untyped
def approve_claim(claim_id: str, card_number: str) -> dict[str, Any]:
    return {
        "claim_id": claim_id,
        "request_id": context.get("request_id"),
        "tenant_id": context.get("tenant_id"),
    }


@app.task(name="claims.explode")  # type: ignore[untyped-decorator]  # celery is untyped
def explode() -> None:
    raise RuntimeError("worker crashed")


@pytest.fixture(scope="module")
def running_worker() -> Iterator[None]:
    """One worker for the module: signals and providers are process-wide anyway."""
    instrument_celery()
    with start_worker(app, pool="solo", perform_ping_check=False, shutdown_timeout=10):
        yield


@pytest.fixture
def worker(running_worker: None, telemetry: Any) -> None:
    telemetry.configure()


def consumer_spans(spans: list[Any]) -> list[Any]:
    return [span for span in spans if span.kind is SpanKind.CONSUMER]


@pytest.mark.usefixtures("worker")
def test_trace_and_request_context_reach_the_worker(telemetry: Any) -> None:
    with (
        get_tracer(__name__).start_as_current_span("approve request") as publisher,
        context.scope(request_id="req-celery-1", tenant_id="tenant-7"),
    ):
        result = approve_claim.delay("CLM-1", "4111111111111111").get(timeout=10)
    [span] = consumer_spans(telemetry.spans())

    assert result == {"claim_id": "CLM-1", "request_id": "req-celery-1", "tenant_id": "tenant-7"}
    assert span.name == "process claims.approve"
    assert span.context.trace_id == publisher.get_span_context().trace_id
    assert span.attributes["messaging.system"] == "celery"


@pytest.mark.usefixtures("worker")
def test_task_arguments_are_never_recorded(telemetry: Any) -> None:
    approve_claim.delay("CLM-2", "4111111111111111").get(timeout=10)
    [span] = consumer_spans(telemetry.spans())

    assert "4111111111111111" not in repr(dict(span.attributes))
    assert span.events == ()


@pytest.mark.usefixtures("worker")
def test_tasks_without_request_context_get_a_fresh_request_id() -> None:
    result = approve_claim.delay("CLM-3", "0").get(timeout=10)

    assert len(result["request_id"]) == 32


@pytest.mark.usefixtures("worker")
def test_failed_tasks_mark_the_span_and_metric(telemetry: Any) -> None:
    with pytest.raises(RuntimeError, match="worker crashed"):
        explode.delay().get(timeout=10)
    spans = consumer_spans(telemetry.spans())
    [point] = telemetry.metrics.points("messaging.process.duration")

    assert spans[0].status.status_code is StatusCode.ERROR
    assert spans[0].attributes["error.type"] == "RuntimeError"
    assert point.attributes["error.type"] == "RuntimeError"


@pytest.mark.usefixtures("worker")
def test_context_does_not_leak_between_tasks() -> None:
    with context.scope(tenant_id="tenant-a"):
        approve_claim.delay("CLM-4", "0").get(timeout=10)

    second = approve_claim.delay("CLM-5", "0").get(timeout=10)

    assert second["tenant_id"] is None


def test_instrumenting_twice_connects_once(telemetry: Any) -> None:
    telemetry.configure()
    instrument_celery()
    instrument_celery()

    from celery import signals  # noqa: PLC0415

    receivers = [
        receiver
        for receiver in signals.task_prerun.receivers
        if "pyobservability" in str(receiver[0])
    ]
    assert len(receivers) == 1
