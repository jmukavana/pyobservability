from typing import TYPE_CHECKING

from pyobservability.audit.delivery import (
    ConfiguredExporter,
    DeadLetterSink,
    FileDeadLetters,
    LogDeadLetters,
)
from pyobservability.audit.exporters import ChainingExporter, audit_exporters
from pyobservability.audit.integrity import ChainSigner
from pyobservability.audit.outcomes import DeliveryOutcomes
from pyobservability.audit.service import AuditService, active_service
from pyobservability.audit.wal import WriteAheadLog
from pyobservability.config import AuditConfig, ObservabilityConfig
from pyobservability.security import Redactor

if TYPE_CHECKING:
    from pyobservability.audit.outbox import Outbox


class AuditComponent:
    name = "audit"

    def __init__(self, config: ObservabilityConfig, redactor: Redactor) -> None:
        self._config = config
        self._redactor = redactor

    def start(self) -> None:
        audit = self._config.auditing
        exporters = [
            ConfiguredExporter(audit_exporters.create(spec.name, **spec.options), spec.required)
            for spec in audit.exporters
        ]
        signer = ChainSigner.from_config(audit.integrity)
        for target in exporters:
            if isinstance(target.exporter, ChainingExporter):
                target.exporter.use_signer(signer)
        outcomes = DeliveryOutcomes(_dead_letters_for(audit), audit.on_delivery_failure)
        service = AuditService(
            self._config, self._redactor, exporters, outcomes, _durable_store_for(audit)
        )
        service.start()
        active_service.current = service

    def shutdown(self, timeout_s: float) -> None:
        service, active_service.current = active_service.current, None
        if service is not None:
            service.shutdown(timeout_s)


def _durable_store_for(audit: AuditConfig) -> "WriteAheadLog | Outbox | None":
    durability = audit.durability
    if durability.boundary == "outbox" and durability.outbox is not None:
        from pyobservability.audit.outbox import Outbox  # noqa: PLC0415 - optional extra

        return Outbox(durability.outbox)
    if durability.boundary == "disk" and durability.spool_dir is not None:
        return WriteAheadLog(durability.spool_dir, fsync=durability.fsync)
    return None


def _dead_letters_for(audit: AuditConfig) -> DeadLetterSink:
    spool_dir = audit.durability.spool_dir
    if audit.retry.dead_letter == "file" and spool_dir is not None:
        return FileDeadLetters(spool_dir)
    return LogDeadLetters()
