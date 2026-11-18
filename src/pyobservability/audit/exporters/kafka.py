import asyncio
import threading
from collections.abc import Callable, Coroutine, Sequence
from typing import Any, Protocol
from uuid import UUID

from pyobservability.audit.models import ExportResult, Failure
from pyobservability.audit.schemas import AuditEvent
from pyobservability.exceptions import MissingExtraError

try:
    from aiokafka import AIOKafkaProducer
    from aiokafka.errors import MessageSizeTooLargeError
except ImportError as error:
    raise MissingExtraError("kafka", "the Kafka audit exporter") from error

DEFAULT_TOPIC = "audit-events"
UNKEYED = b"_global"
_MESSAGE_ITSELF_IS_BAD = (MessageSizeTooLargeError,)


class Producer(Protocol):
    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def send(
        self, topic: str, *, value: bytes, key: bytes, headers: list[tuple[str, bytes]]
    ) -> "asyncio.Future[Any]": ...


ProducerFactory = Callable[..., Producer]


class KafkaAuditExporter:
    """Publishes audit events with `acks=all` and idempotence, keyed by tenant for ordering.

    aiokafka is asynchronous and exporters are not, so the producer lives on a private event loop.
    """

    name = "kafka"

    def __init__(
        self,
        bootstrap_servers: str | list[str],
        *,
        topic: str = DEFAULT_TOPIC,
        send_timeout_s: float = 30.0,
        producer_factory: ProducerFactory = AIOKafkaProducer,
        **producer_options: Any,
    ) -> None:
        self._topic = topic
        self._send_timeout_s = send_timeout_s
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever, name="pyobservability-kafka", daemon=True
        )
        self._thread.start()
        self._producer_factory = producer_factory
        self._producer_settings = {
            "bootstrap_servers": bootstrap_servers,
            "acks": "all",
            "enable_idempotence": True,
            **producer_options,
        }
        self._producer: Producer | None = None

    def export(self, events: Sequence[AuditEvent]) -> ExportResult:
        try:
            failures = self._on_loop(self._publish(events))
        except Exception as error:
            return ExportResult.all_failed(events, f"Kafka unavailable: {error}")
        return ExportResult(failures)

    def shutdown(self) -> None:
        if self._producer is not None:
            self._on_loop(self._producer.stop())
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join()
        self._loop.close()

    async def _publish(self, events: Sequence[AuditEvent]) -> dict[UUID, Failure]:
        producer = await self._started_producer()
        sends = [await producer.send(self._topic, **_message(event)) for event in events]
        outcomes = await asyncio.gather(*sends, return_exceptions=True)
        return {
            event.event_id: _failure(outcome)
            for event, outcome in zip(events, outcomes, strict=True)
            if isinstance(outcome, BaseException)
        }

    async def _started_producer(self) -> Producer:
        """aiokafka binds a producer to the running loop and cannot restart one that failed."""
        if self._producer is not None:
            return self._producer
        producer = self._producer_factory(**self._producer_settings)
        try:
            await producer.start()
        except BaseException:
            await producer.stop()
            raise
        self._producer = producer
        return producer

    def _on_loop[T](self, coroutine: Coroutine[Any, Any, T]) -> T:
        return asyncio.run_coroutine_threadsafe(coroutine, self._loop).result(self._send_timeout_s)


def _message(event: AuditEvent) -> dict[str, Any]:
    return {
        "value": event.model_dump_json().encode(),
        "key": event.tenant_id.encode() if event.tenant_id else UNKEYED,
        "headers": [
            ("event_id", str(event.event_id).encode()),
            ("event_type", event.event_type.encode()),
            ("schema_version", event.schema_version.encode()),
        ],
    }


def _failure(error: BaseException) -> Failure:
    """Delivery is idempotent on `event_id`, so anything not about the message itself retries."""
    retryable = not isinstance(error, _MESSAGE_ITSELF_IS_BAD)
    return Failure(f"Kafka rejected the event: {type(error).__name__}: {error}", retryable)
