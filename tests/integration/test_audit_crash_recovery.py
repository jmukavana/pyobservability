import os
import signal
import subprocess
import sys
import textwrap
from collections.abc import Sequence
from pathlib import Path
from uuid import UUID

import pytest

import pyobservability
from pyobservability import audit
from pyobservability.audit import AuditEvent, ExportResult, audit_exporters

EVENTS_BEFORE_CRASH = 25
COLLECTOR = "crash-collector"

CRASHING_PROCESS = textwrap.dedent(
    """
    import sys, threading, warnings
    warnings.simplefilter("ignore")
    import pyobservability
    from pyobservability import audit
    from pyobservability.audit import audit_exporters

    class NeverDelivers:
        name = "never"
        def export(self, events):
            threading.Event().wait()
        def shutdown(self):
            pass

    audit_exporters.register("never", NeverDelivers)
    pyobservability.configure(
        service_name="crashing",
        logging={"enabled": False}, tracing={"enabled": False}, metrics={"enabled": False},
        auditing={
            "exporters": [{"name": "never"}],
            "durability": {"boundary": "disk", "spool_dir": sys.argv[1]},
        },
    )
    for index in range(int(sys.argv[2])):
        receipt = audit.record(
            event_type="CLAIM_APPROVED", resource_type="CLAIM",
            resource_id=f"CLM-{index}", action="APPROVE",
        )
        print(receipt.event_id, flush=True)
    print("ready", flush=True)
    threading.Event().wait()
    """
)


class Collector:
    received: list[AuditEvent] = []
    name = COLLECTOR

    def export(self, events: Sequence[AuditEvent]) -> ExportResult:
        Collector.received.extend(events)
        return ExportResult.success()

    def shutdown(self) -> None:
        pass


if COLLECTOR not in audit_exporters.names():
    audit_exporters.register(COLLECTOR, Collector)


def spool_then_crash(spool: Path) -> list[UUID]:
    """Each printed ID is a returned receipt: the crash happens after `record()` promised."""
    acknowledged = []
    with subprocess.Popen(  # noqa: S603 - runs this interpreter on a fixed script
        [sys.executable, "-c", CRASHING_PROCESS, str(spool), str(EVENTS_BEFORE_CRASH)],
        stdout=subprocess.PIPE,
        text=True,
    ) as process:
        assert process.stdout is not None
        for line in process.stdout:
            if line.strip() == "ready":
                break
            acknowledged.append(UUID(line.strip()))
        os.kill(process.pid, signal.SIGKILL)
        process.wait(timeout=10)
    return acknowledged


@pytest.mark.integration
@pytest.mark.skipif(sys.platform == "win32", reason="SIGKILL semantics")
def test_spooled_events_survive_kill_9(tmp_path: Path) -> None:
    acknowledged = spool_then_crash(tmp_path / "spool")
    Collector.received = []

    pyobservability.configure(
        service_name="restarted",
        logging={"enabled": False},
        tracing={"enabled": False},
        metrics={"enabled": False},
        auditing={
            "exporters": [{"name": COLLECTOR}],
            "durability": {"boundary": "disk", "spool_dir": str(tmp_path / "spool")},
        },
    )
    assert audit.flush(timeout_s=10)

    assert len(acknowledged) == EVENTS_BEFORE_CRASH
    assert [event.event_id for event in Collector.received] == acknowledged
