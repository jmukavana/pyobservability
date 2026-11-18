import asyncio
import os
import socket
import time
from collections.abc import Callable, Coroutine, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from opentelemetry import metrics
from opentelemetry.metrics import CallbackOptions, Observation
from pydantic import TypeAdapter, ValidationError

from pyobservability.audit.delivery import (
    Admission,
    ConfiguredExporter,
    Delivery,
    DeliveryInterrupted,
    RetrySchedule,
    Wait,
    deadline_wait,
)
from pyobservability.audit.dispatcher import Dispatcher
from pyobservability.audit.exporters import ChainingExporter
from pyobservability.audit.integrity import GLOBAL_CHAIN, VerifyResult
from pyobservability.audit.models import AuditReceipt
from pyobservability.audit.outcomes import AUDIT_METER, DeliveryOutcomes
from pyobservability.audit.schemas import (
    ActorType,
    AuditEvent,
    Category,
    Change,
    Outcome,
    Source,
)
from pyobservability.audit.wal import WriteAheadLog
from pyobservability.config import ObservabilityConfig
from pyobservability.context import current_ids, get
from pyobservability.context.correlation import TENANT_ID, USER_ID
from pyobservability.exceptions import (
    AuditDurabilityError,
    AuditValidationError,
    ConfigurationError,
    NotConfiguredError,
)
from pyobservability.security import REDACTION_FAILED, Redactor

if TYPE_CHECKING:
    from pyobservability.audit.outbox import Outbox

REDACTION_ERROR_FLAG = "_redaction_error"
CHAIN_VERIFIED_EVENT = "AUDIT_CHAIN_VERIFIED"
_CHANGES = TypeAdapter(dict[str, Change])


class AuditService:
    def __init__(
        self,
        config: ObservabilityConfig,
        redactor: Redactor,
        exporters: Sequence[ConfiguredExporter],
        outcomes: DeliveryOutcomes,
        store: "WriteAheadLog | Outbox | None",
    ) -> None:
        self._audit = config.auditing
        self._redactor = redactor
        self._exporters = tuple(exporters)
        self._outcomes = outcomes
        self._wal = store if isinstance(store, WriteAheadLog) else None
        self._outbox = None if store is None or isinstance(store, WriteAheadLog) else store
        self._schedule = RetrySchedule(self._audit.retry)
        self._source = Source(
            service=config.service_name,
            version=config.service_version,
            environment=config.environment,
            host=socket.gethostname(),
        )
        self.admission = Admission(
            self._audit.queue_max_size,
            blocks=self._audit.on_queue_full == "block",
            timeout_s=self._audit.block_timeout_s,
        )
        delivery = Delivery(self._exporters, self._schedule)
        self._relay = (
            self._outbox.relay(delivery, outcomes, self._audit.batch_size) if self._outbox else None
        )
        background = not self._audit.delivers_inline and self._relay is None
        self._dispatcher = self._new_dispatcher(delivery) if background else None
        self._started_in_process = os.getpid()

    def start(self) -> None:
        if self._relay is not None:
            self._relay.start()
        recovered = self._wal.open() if self._wal is not None else []
        if self._dispatcher is None:
            return
        self.admission.admit_recovered(len(recovered))
        self._dispatcher.start()
        for event in recovered:
            self._dispatcher.submit(event)

    def record(self, fields: Mapping[str, Any], transaction: object | None = None) -> AuditReceipt:
        self._require_the_starting_process()
        if transaction is not None and self._outbox is None:
            raise ConfigurationError("transaction= needs auditing.durability.boundary='outbox'")
        event = self._build(fields)
        self._outcomes.recorded(event)
        if self._outbox is not None and self._relay is not None:
            self._outbox.write(event, transaction)
            self._relay.nudge()
            return AuditReceipt(event.event_id, "stored")
        if self._dispatcher is None:
            self._deliver_inline(event)
            return AuditReceipt(event.event_id, "delivered")
        self._admit_durably(event)
        self._dispatcher.submit(event)
        return AuditReceipt(event.event_id, "spooled" if self._wal else "accepted")

    def _require_the_starting_process(self) -> None:
        """Delivery threads do not survive `fork()`; recording here would queue events forever."""
        if os.getpid() != self._started_in_process:
            raise AuditDurabilityError(
                "audit was configured before this process was forked, so its delivery thread "
                "does not exist here; call pyobservability.configure() in each worker process "
                "(gunicorn post_fork, Celery worker_process_init)"
            )

    def flush(self, timeout_s: float) -> bool:
        if self._relay is not None:
            return self._relay.wait_until_empty(timeout_s)
        return self.admission.wait_until_idle(timeout_s)

    def verify(self, tenant_id: str | None, exporter_name: str) -> VerifyResult:
        """Checks one tenant's chain; reading the audit trail is itself an audited access."""
        store = next(
            (
                target.exporter
                for target in self._exporters
                if target.exporter.name == exporter_name
            ),
            None,
        )
        if not isinstance(store, ChainingExporter):
            raise ConfigurationError(f"audit exporter {exporter_name!r} keeps no hash chain")
        result = store.verify(tenant_id)
        self.record(
            {
                "event_type": CHAIN_VERIFIED_EVENT,
                "category": "access",
                "outcome": "success" if result.ok else "failure",
                "resource_type": "AUDIT_CHAIN",
                "resource_id": tenant_id or GLOBAL_CHAIN,
                "action": "VERIFY",
                "tenant_id": tenant_id,
                "metadata": {
                    "checked": result.checked,
                    "first_bad_seq": result.first_bad_seq,
                    "problem": result.problem,
                },
            }
        )
        return result

    def shutdown(self, timeout_s: float) -> None:
        if self._dispatcher is not None:
            self._dispatcher.shutdown(timeout_s)
        if self._relay is not None:
            self._relay.shutdown(timeout_s)
        for target in self._exporters:
            target.exporter.shutdown()
        if self._wal is not None:
            self._wal.close()

    def _new_dispatcher(self, delivery: Delivery) -> Dispatcher:
        return Dispatcher(
            delivery,
            self.admission,
            self._outcomes,
            self._wal,
            batch_size=self._audit.batch_size,
        )

    def _admit_durably(self, event: AuditEvent) -> None:
        self.admission.admit()
        if self._wal is None:
            return
        try:
            self._wal.append(event)
        except OSError as error:
            self.admission.release(1)
            raise AuditDurabilityError(f"audit event could not be spooled: {error}") from error

    def _deliver_inline(self, event: AuditEvent) -> None:
        wait = deadline_wait(time.monotonic() + self._audit.block_timeout_s)
        delivery = Delivery(self._exporters, self._schedule)
        for target in self._exporters:
            reasons = self._deliver_before_deadline(delivery, target, event, wait)
            if not reasons:
                continue
            reason = f"{target.exporter.name}: {reasons[event.event_id]}"
            if target.required:
                raise AuditDurabilityError(f"audit event {event.event_id} not delivered: {reason}")
            self._outcomes.dead_lettered([event], reason)
        self._outcomes.delivered([event])

    def _deliver_before_deadline(
        self, delivery: Delivery, target: ConfiguredExporter, event: AuditEvent, wait: Wait
    ) -> dict[UUID, str]:
        try:
            return delivery.deliver_to(target, [event], wait)
        except DeliveryInterrupted:
            timeout = self._audit.block_timeout_s
            return {event.event_id: f"not delivered within block_timeout_s={timeout}"}

    def _build(self, fields: Mapping[str, Any]) -> AuditEvent:
        provided = {name: value for name, value in fields.items() if value is not None}
        values = {**self._context_defaults(), **provided, "source": self._source}
        values["changes"], changes_failed = self._redacted_changes(provided.get("changes", {}))
        values["metadata"] = self._redactor.redact_fields(provided.get("metadata", {}))
        if changes_failed or REDACTION_FAILED in values["metadata"].values():
            values["metadata"] = {**values["metadata"], REDACTION_ERROR_FLAG: True}
        if self._audit.tenant_required and not values.get("tenant_id"):
            raise AuditValidationError("tenant_id is required for audit events")
        try:
            return AuditEvent(**values)
        except ValidationError as error:
            raise AuditValidationError(f"invalid audit event:\n{error}") from error

    def _context_defaults(self) -> dict[str, Any]:
        ids = current_ids()
        return {
            "actor_id": _text_or_none(get(USER_ID)),
            "tenant_id": _text_or_none(get(TENANT_ID)),
            "request_id": ids.get("request_id"),
            "trace_id": ids.get("trace_id"),
            "span_id": ids.get("span_id"),
        }

    def _redacted_changes(self, raw: Any) -> tuple[dict[str, Change], bool]:
        try:
            changes = _CHANGES.validate_python(raw)
        except ValidationError as error:
            raise AuditValidationError(f"invalid audit changes:\n{error}") from error
        olds = self._redactor.redact_fields({name: change.old for name, change in changes.items()})
        news = self._redactor.redact_fields({name: change.new for name, change in changes.items()})
        failed = REDACTION_FAILED in (*olds.values(), *news.values())
        redacted = {
            name: Change(old=olds.get(name), new=news.get(name))
            for name in changes
            if name in olds or name in news
        }
        return redacted, failed


def _text_or_none(value: object) -> str | None:
    return None if value is None else str(value)


@dataclass(slots=True)
class _ActiveService:
    current: AuditService | None = None


active_service = _ActiveService()


def _queue_depth(_: CallbackOptions) -> Iterable[Observation]:
    service = active_service.current
    return [] if service is None else [Observation(service.admission.in_flight)]


metrics.get_meter(AUDIT_METER).create_observable_gauge(
    "pyobs.audit.queue.depth", callbacks=[_queue_depth], unit="{event}"
)


def _service() -> AuditService:
    service = active_service.current
    if service is None:
        raise NotConfiguredError(
            "audit events need pyobservability.configure() with auditing enabled; "
            "they are never silently dropped"
        )
    return service


def record(  # noqa: PLR0913 - keyword-only fields mirror the audit event schema
    *,
    event_type: str,
    resource_type: str,
    resource_id: str,
    action: str,
    actor_id: str | None = None,
    actor_type: ActorType = "user",
    tenant_id: str | None = None,
    category: Category = "business",
    outcome: Outcome = "success",
    changes: Mapping[str, Mapping[str, Any]] | None = None,
    metadata: Mapping[str, Any] | None = None,
    occurred_at: datetime | None = None,
    event_id: UUID | None = None,
    transaction: object | None = None,
) -> AuditReceipt:
    """Records an audit event; returning means the configured durability boundary was reached.

    With `durability.boundary="outbox"`, pass the caller's database `transaction` (a psycopg
    connection, or a SQLAlchemy Session/Connection) and the event commits or rolls back with it.
    """
    return _service().record(
        {
            "event_type": event_type,
            "resource_type": resource_type,
            "resource_id": resource_id,
            "action": action,
            "actor_id": actor_id,
            "actor_type": actor_type,
            "tenant_id": tenant_id,
            "category": category,
            "outcome": outcome,
            "changes": changes,
            "metadata": metadata,
            "occurred_at": occurred_at,
            "event_id": event_id,
        },
        transaction,
    )


def _in_worker_thread[**P, R](function: Callable[P, R]) -> Callable[P, Coroutine[Any, Any, R]]:
    async def run_in_thread(*args: P.args, **kwargs: P.kwargs) -> R:
        return await asyncio.to_thread(function, *args, **kwargs)

    run_in_thread.__doc__ = f"Async {function.__name__}: runs in a worker thread, off the loop."
    return run_in_thread


arecord = _in_worker_thread(record)


def flush(timeout_s: float = 10.0) -> bool:
    """Waits until every accepted event was delivered or dead-lettered."""
    return _service().flush(timeout_s)


def verify(tenant_id: str | None = None, *, exporter: str = "postgres") -> VerifyResult:
    """Verifies one tenant's tamper-evident chain in a chaining audit store."""
    return _service().verify(tenant_id, exporter)
