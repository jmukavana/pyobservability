from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal
from uuid import UUID

from pyobservability.audit.schemas import AuditEvent

Durability = Literal["accepted", "spooled", "delivered", "stored"]
"""`stored`: written to the outbox, in the caller's transaction when one was given."""


@dataclass(frozen=True, slots=True)
class AuditReceipt:
    event_id: UUID
    durability: Durability


@dataclass(frozen=True, slots=True)
class Failure:
    reason: str
    retryable: bool = True


@dataclass(frozen=True, slots=True)
class ExportResult:
    """Per-event outcome of an export; events without a failure entry were delivered."""

    failures: Mapping[UUID, Failure] = field(default_factory=lambda: MappingProxyType({}))

    @classmethod
    def success(cls) -> "ExportResult":
        return cls()

    @classmethod
    def all_failed(
        cls, events: Iterable[AuditEvent], reason: str, *, retryable: bool = True
    ) -> "ExportResult":
        failure = Failure(reason, retryable)
        return cls(MappingProxyType({event.event_id: failure for event in events}))
