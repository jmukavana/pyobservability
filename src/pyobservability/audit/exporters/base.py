from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from pyobservability.audit.integrity import ChainSigner, VerifyResult
from pyobservability.audit.models import ExportResult
from pyobservability.audit.schemas import AuditEvent


class AuditExporter(Protocol):
    """Delivers audit events. Called from one dispatcher thread, or from callers when inline.

    Report per-event failures in the result; raising marks the whole batch as retryable.
    Implementations must be thread-safe and idempotent on `event_id`.
    """

    @property
    def name(self) -> str: ...

    def export(self, events: Sequence[AuditEvent]) -> ExportResult: ...

    def shutdown(self) -> None: ...


@runtime_checkable
class ChainingExporter(Protocol):
    """Optional capability: the store links events into a tamper-evident hash chain."""

    def use_signer(self, signer: ChainSigner) -> None: ...

    def verify(self, tenant_id: str | None) -> VerifyResult: ...
