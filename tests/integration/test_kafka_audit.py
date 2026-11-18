import asyncio
import json
import os
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from aiokafka.errors import KafkaConnectionError, KafkaTimeoutError, MessageSizeTooLargeError

import pyobservability
from pyobservability import audit
from pyobservability.audit import AuditEvent
from pyobservability.audit.exporters.kafka import KafkaAuditExporter

LIVE_BROKER = os.environ.get("KAFKA_TEST_BOOTSTRAP")
SOURCE = {"service": "claims", "version": "1", "environment": "test", "host": "h"}


class FakeProducer:
    """Same async surface as AIOKafkaProducer; failures are keyed by resource ID."""

    latest: "FakeProducer | None" = None
    failures: dict[str, BaseException] = {}
    start_error: BaseException | None = None

    def __init__(self, **settings: Any) -> None:
        self.settings = settings
        self.sent: list[dict[str, Any]] = []
        self.stopped = False
        FakeProducer.latest = self

    async def start(self) -> None:
        if FakeProducer.start_error is not None:
            raise FakeProducer.start_error

    async def stop(self) -> None:
        self.stopped = True

    async def send(
        self, topic: str, *, value: bytes, key: bytes, headers: list[tuple[str, bytes]]
    ) -> "asyncio.Future[Any]":
        delivered: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        payload = json.loads(value)
        if (error := FakeProducer.failures.get(payload["resource_id"])) is not None:
            delivered.set_exception(error)
        else:
            delivered.set_result(None)
            self.sent.append({"topic": topic, "key": key, "headers": dict(headers), **payload})
        return delivered


@pytest.fixture(autouse=True)
def reset_fake() -> Iterator[None]:
    FakeProducer.latest, FakeProducer.failures, FakeProducer.start_error = None, {}, None
    yield


@pytest.fixture
def exporter() -> Iterator[KafkaAuditExporter]:
    kafka = KafkaAuditExporter(
        "broker-1:9092", topic="claims-audit", producer_factory=FakeProducer, linger_ms=5
    )
    yield kafka
    kafka.shutdown()


def producer() -> FakeProducer:
    assert FakeProducer.latest is not None
    return FakeProducer.latest


def event(resource_id: str = "CLM-1", tenant_id: str | None = "tenant-1") -> AuditEvent:
    return AuditEvent.model_validate(
        {
            "event_type": "CLAIM_APPROVED",
            "resource_type": "CLAIM",
            "resource_id": resource_id,
            "action": "APPROVE",
            "tenant_id": tenant_id,
            "source": SOURCE,
        }
    )


def test_producer_is_durable_and_idempotent(exporter: KafkaAuditExporter) -> None:
    exporter.export([event()])

    assert producer().settings == {
        "bootstrap_servers": "broker-1:9092",
        "acks": "all",
        "enable_idempotence": True,
        "linger_ms": 5,
    }


def test_events_are_keyed_by_tenant_with_identifying_headers(
    exporter: KafkaAuditExporter,
) -> None:
    published = event()

    assert exporter.export([published]).failures == {}
    [message] = producer().sent

    assert message["topic"] == "claims-audit"
    assert message["key"] == b"tenant-1"
    assert message["headers"]["event_id"] == str(published.event_id).encode()
    assert message["event_type"] == "CLAIM_APPROVED"


def test_events_without_tenant_share_one_key(exporter: KafkaAuditExporter) -> None:
    exporter.export([event(tenant_id=None)])

    assert producer().sent[0]["key"] == b"_global"


@pytest.mark.parametrize(
    ("error", "retryable"),
    [
        (KafkaTimeoutError(), True),
        (KafkaConnectionError("broker gone"), True),
        (MessageSizeTooLargeError(), False),
    ],
)
def test_failures_are_classified(
    exporter: KafkaAuditExporter, error: BaseException, retryable: bool
) -> None:
    FakeProducer.failures = {"CLM-bad": error}
    bad = event("CLM-bad")

    result = exporter.export([event("CLM-ok"), bad])

    assert list(result.failures) == [bad.event_id]
    assert result.failures[bad.event_id].retryable is retryable
    assert [message["resource_id"] for message in producer().sent] == ["CLM-ok"]


def test_unreachable_broker_makes_the_whole_batch_retryable() -> None:
    FakeProducer.start_error = KafkaConnectionError("no brokers")
    kafka = KafkaAuditExporter("broker-1:9092", producer_factory=FakeProducer)
    events = [event("A"), event("B")]

    result = kafka.export(events)
    kafka.shutdown()

    assert set(result.failures) == {item.event_id for item in events}
    assert all(failure.retryable for failure in result.failures.values())


def test_shutdown_stops_the_producer_and_its_loop() -> None:
    kafka = KafkaAuditExporter("broker-1:9092", producer_factory=FakeProducer)
    kafka.export([event()])

    kafka.shutdown()

    assert producer().stopped
    assert not kafka._thread.is_alive()


def test_kafka_is_selectable_by_name() -> None:
    pyobservability.configure(
        service_name="claims",
        logging={"enabled": False},
        tracing={"enabled": False},
        metrics={"enabled": False},
        auditing={
            "delivery": "sync",
            "durability": {"boundary": "exporter"},
            "exporters": [
                {
                    "name": "kafka",
                    "options": {"bootstrap_servers": "b:9092", "producer_factory": FakeProducer},
                }
            ],
        },
    )

    receipt = audit.record(
        event_type="CLAIM_APPROVED", resource_type="CLAIM", resource_id="CLM-9", action="APPROVE"
    )

    assert receipt.durability == "delivered"
    assert producer().sent[0]["event_id"] == str(receipt.event_id)


@pytest.mark.integration
@pytest.mark.skipif(LIVE_BROKER is None, reason="set KAFKA_TEST_BOOTSTRAP to a test broker")
def test_events_reach_a_real_broker() -> None:
    from aiokafka import AIOKafkaConsumer  # noqa: PLC0415

    topic = f"audit-{uuid.uuid4().hex[:8]}"
    published = event()
    kafka = KafkaAuditExporter(str(LIVE_BROKER), topic=topic)
    assert kafka.export([published]).failures == {}
    kafka.shutdown()

    async def consume_one() -> bytes:
        consumer = AIOKafkaConsumer(
            topic, bootstrap_servers=LIVE_BROKER, auto_offset_reset="earliest"
        )
        await consumer.start()
        try:
            message = await asyncio.wait_for(consumer.getone(), timeout=30)
            return bytes(message.value)
        finally:
            await consumer.stop()

    assert json.loads(asyncio.run(consume_one()))["event_id"] == str(published.event_id)
