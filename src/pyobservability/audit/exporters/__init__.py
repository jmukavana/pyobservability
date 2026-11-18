"""Loss-intolerant audit exporters; never shared with telemetry exporters."""

from typing import Any

from pyobservability.audit.exporters.base import AuditExporter, ChainingExporter
from pyobservability.audit.exporters.console import ConsoleAuditExporter
from pyobservability.audit.exporters.file import JsonLinesAuditExporter
from pyobservability.core.registry import Registry

POSTGRES = "postgres"
KAFKA = "kafka"


def _postgres_exporter(**options: Any) -> AuditExporter:
    from pyobservability.audit.exporters.postgres import (  # noqa: PLC0415 - optional extra
        PostgresAuditExporter,
    )

    return PostgresAuditExporter(**options)


def _kafka_exporter(**options: Any) -> AuditExporter:
    from pyobservability.audit.exporters.kafka import (  # noqa: PLC0415 - optional extra
        KafkaAuditExporter,
    )

    return KafkaAuditExporter(**options)


audit_exporters: Registry[AuditExporter] = Registry(
    "audit exporter", entry_point_group="pyobservability.audit_exporters"
)
audit_exporters.register(ConsoleAuditExporter.name, ConsoleAuditExporter)
audit_exporters.register(JsonLinesAuditExporter.name, JsonLinesAuditExporter)
audit_exporters.register(POSTGRES, _postgres_exporter)
audit_exporters.register(KAFKA, _kafka_exporter)

__all__ = [
    "AuditExporter",
    "ChainingExporter",
    "ConsoleAuditExporter",
    "JsonLinesAuditExporter",
    "audit_exporters",
]
