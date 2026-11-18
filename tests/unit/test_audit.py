import io
import json
import threading
import time
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import RFC_4122, UUID, uuid4

import pytest
from opentelemetry.sdk.trace import TracerProvider

import pyobservability
from pyobservability import (
    AuditDurabilityError,
    AuditValidationError,
    ConfigurationError,
    NotConfiguredError,
    audit,
    context,
)
from pyobservability.audit import AuditEvent, ExportResult, Failure, audit_exporters
from pyobservability.audit.delivery import DEAD_LETTER_FILE, RetrySchedule
from pyobservability.audit.exporters import ConsoleAuditExporter, JsonLinesAuditExporter
from pyobservability.audit.pg import validated_identifier
from pyobservability.audit.schemas import uuid7
from pyobservability.audit.wal import LOG_FILE, WriteAheadLog
from pyobservability.config import RetryConfig

RECORDING = "recording"
FAST_RETRY = {"initial_backoff_s": 0.001, "max_backoff_s": 0.005, "max_attempts": 3}


class RecordingExporter:
    """Test double: optionally fails, refuses permanently or blocks until released."""

    instances: list["RecordingExporter"] = []

    def __init__(
        self,
        name: str = RECORDING,
        fail_first: int = 0,
        permanent: bool = False,
        raises: bool = False,
        blocked: bool = False,
    ) -> None:
        self.name = name
        self.delivered: list[AuditEvent] = []
        self.attempts = 0
        self._fail_first = fail_first
        self._permanent = permanent
        self._raises = raises
        self.gate = threading.Event()
        if not blocked:
            self.gate.set()
        RecordingExporter.instances.append(self)

    def export(self, events: Sequence[AuditEvent]) -> ExportResult:
        self.gate.wait()
        self.attempts += 1
        if self._raises and self.attempts <= self._fail_first:
            raise ConnectionError("broker unreachable")
        if self._permanent:
            return ExportResult.all_failed(events, "rejected by schema", retryable=False)
        if self.attempts <= self._fail_first:
            return ExportResult.all_failed(events, "temporarily unavailable")
        self.delivered.extend(events)
        return ExportResult.success()

    def shutdown(self) -> None:
        self.gate.set()


if RECORDING not in audit_exporters.names():
    audit_exporters.register(RECORDING, RecordingExporter)


@pytest.fixture(autouse=True)
def fresh_exporters() -> Iterator[None]:
    RecordingExporter.instances.clear()
    yield
    for exporter in RecordingExporter.instances:
        exporter.gate.set()


def exporter(position: int = 0) -> RecordingExporter:
    return RecordingExporter.instances[position]


def configure_audit(
    *exporters: dict[str, Any], shutdown_timeout_s: float = 10.0, **auditing: Any
) -> None:
    pyobservability.configure(
        service_name="claims-service",
        shutdown_timeout_s=shutdown_timeout_s,
        service_version="3.1.0",
        logging={"enabled": False},
        tracing={"enabled": False},
        metrics={"enabled": False},
        auditing={
            "exporters": list(exporters) or [{"name": RECORDING}],
            "retry": FAST_RETRY,
            **auditing,
        },
    )


def disk(spool_dir: Path, **extra: Any) -> dict[str, Any]:
    return {"durability": {"boundary": "disk", "spool_dir": str(spool_dir)}, **extra}


def inline(**extra: Any) -> dict[str, Any]:
    return {"delivery": "sync", "durability": {"boundary": "exporter"}, **extra}


def approve(**overrides: Any) -> audit.AuditReceipt:
    fields: dict[str, Any] = {
        "event_type": "CLAIM_APPROVED",
        "resource_type": "CLAIM",
        "resource_id": "CLM-2001",
        "action": "APPROVE",
        "actor_id": "USR-1001",
        **overrides,
    }
    return audit.record(**fields)


def delivered_events() -> list[AuditEvent]:
    assert audit.flush(timeout_s=5)
    return exporter().delivered


@pytest.fixture
def quiet_memory_audit() -> Iterator[None]:
    with pytest.warns(pyobservability.PyObservabilityWarning):
        configure_audit()
    yield


class TestRecording:
    @pytest.mark.usefixtures("quiet_memory_audit")
    def test_event_carries_the_stable_schema(self) -> None:
        receipt = approve(changes={"status": {"old": "PENDING", "new": "APPROVED"}})
        [event] = delivered_events()

        assert event.event_id == receipt.event_id
        assert event.event_id.version == 7
        assert (event.event_type, event.action, event.resource_id) == (
            "CLAIM_APPROVED",
            "APPROVE",
            "CLM-2001",
        )
        assert event.changes["status"].model_dump() == {"old": "PENDING", "new": "APPROVED"}
        assert event.source.service == "claims-service"
        assert event.source.version == "3.1.0"

    @pytest.mark.usefixtures("quiet_memory_audit")
    def test_context_fills_in_actor_tenant_and_correlation(self) -> None:
        tracer = TracerProvider().get_tracer(__name__)

        with (
            context.scope(user_id="USR-7", tenant_id="tenant-9", request_id="req-12345678"),
            tracer.start_as_current_span("approve") as span,
        ):
            approve(actor_id=None)
        [event] = delivered_events()

        assert (event.actor_id, event.tenant_id, event.request_id) == (
            "USR-7",
            "tenant-9",
            "req-12345678",
        )
        assert event.trace_id == format(span.get_span_context().trace_id, "032x")

    @pytest.mark.usefixtures("quiet_memory_audit")
    def test_explicit_fields_override_context(self) -> None:
        with context.scope(tenant_id="from-context"):
            approve(tenant_id="explicit")

        assert delivered_events()[0].tenant_id == "explicit"

    @pytest.mark.usefixtures("quiet_memory_audit")
    def test_caller_supplied_event_id_is_kept_for_idempotency(self) -> None:
        event_id = uuid4()

        assert approve(event_id=event_id).event_id == event_id

    @pytest.mark.usefixtures("quiet_memory_audit")
    async def test_async_record_keeps_context(self) -> None:
        with context.scope(tenant_id="tenant-async"):
            receipt = await audit.arecord(
                event_type="CLAIM_REJECTED",
                resource_type="CLAIM",
                resource_id="CLM-3",
                action="REJECT",
            )
        [event] = delivered_events()

        assert event.event_id == receipt.event_id
        assert event.tenant_id == "tenant-async"


class TestValidation:
    @pytest.fixture(autouse=True)
    def _configured(self, quiet_memory_audit: None) -> None:
        pass

    @pytest.mark.parametrize("event_type", ["claim_approved", "AP", "1BAD", "X" * 65])
    def test_event_type_must_be_upper_snake_case(self, event_type: str) -> None:
        with pytest.raises(AuditValidationError, match="event_type"):
            approve(event_type=event_type)

    def test_empty_resource_id_is_rejected(self) -> None:
        with pytest.raises(AuditValidationError, match="resource_id"):
            approve(resource_id="")

    def test_future_occurrence_is_rejected(self) -> None:
        with pytest.raises(AuditValidationError, match="future"):
            approve(occurred_at=datetime.now(UTC) + timedelta(hours=1))

    def test_naive_timestamps_are_rejected(self) -> None:
        with pytest.raises(AuditValidationError, match="occurred_at"):
            approve(occurred_at=datetime(2026, 1, 1))  # noqa: DTZ001

    def test_oversized_payload_is_rejected(self) -> None:
        with pytest.raises(AuditValidationError, match="exceed"):
            approve(metadata={f"field_{index}": "x" * 1000 for index in range(80)})

    def test_deeply_nested_payload_is_rejected(self) -> None:
        nested: dict[str, Any] = {"leaf": 1}
        for _ in range(8):
            nested = {"child": nested}

        with pytest.raises(AuditValidationError, match="nest deeper"):
            approve(metadata=nested)

    def test_malformed_changes_are_rejected(self) -> None:
        with pytest.raises(AuditValidationError, match="changes"):
            approve(changes={"status": "APPROVED"})

    def test_invalid_events_are_never_delivered(self) -> None:
        with pytest.raises(AuditValidationError):
            approve(event_type="bad")

        assert delivered_events() == []


def test_tenant_can_be_required() -> None:
    with pytest.warns(pyobservability.PyObservabilityWarning):
        configure_audit(tenant_required=True)

    with pytest.raises(AuditValidationError, match="tenant_id is required"):
        approve()
    with context.scope(tenant_id="tenant-1"):
        approve()


class TestNotConfigured:
    def test_recording_before_configure_raises(self) -> None:
        with pytest.raises(NotConfiguredError, match="never silently dropped"):
            approve()

    def test_recording_with_auditing_disabled_raises(self) -> None:
        pyobservability.configure(service_name="svc", auditing={"enabled": False})

        with pytest.raises(NotConfiguredError):
            approve()

    def test_recording_after_shutdown_raises(self, quiet_memory_audit: None) -> None:
        pyobservability.shutdown()

        with pytest.raises(NotConfiguredError):
            approve()


class TestRedaction:
    @pytest.mark.usefixtures("quiet_memory_audit")
    def test_secret_changes_and_metadata_are_masked(self) -> None:
        approve(
            changes={"password": {"old": "hunter2", "new": "s3cret!"}},
            metadata={"note": "rotated Bearer abc.def", "api_key": "k-1"},
        )
        [event] = delivered_events()

        assert event.changes["password"].model_dump() == {"old": "[REDACTED]", "new": "[REDACTED]"}
        assert event.metadata == {"note": "rotated [REDACTED]", "api_key": "[REDACTED]"}

    @pytest.mark.usefixtures("quiet_memory_audit")
    def test_pii_in_changes_is_masked(self) -> None:
        approve(changes={"contact": {"old": "a@old.example", "new": "jane@new.example"}})

        assert delivered_events()[0].changes["contact"].new == "j***@new.example"

    def test_drop_strategy_removes_pii_changes(self) -> None:
        with pytest.warns(pyobservability.PyObservabilityWarning):
            pyobservability.configure(
                service_name="svc",
                logging={"enabled": False},
                tracing={"enabled": False},
                metrics={"enabled": False},
                security={"pii_strategy": "drop"},
                auditing={"exporters": [{"name": RECORDING}]},
            )

        approve(changes={"email": {"old": "a@b.example", "new": "c@d.example"}, "status": {}})

        assert set(delivered_events()[0].changes) == {"status"}


class TestDurabilityBoundaries:
    @pytest.mark.usefixtures("quiet_memory_audit")
    def test_memory_boundary_accepts_then_delivers(self) -> None:
        receipt = approve()

        assert receipt.durability == "accepted"
        assert [event.event_id for event in delivered_events()] == [receipt.event_id]

    def test_disk_boundary_spools_before_returning(self, tmp_path: Path) -> None:
        configure_audit({"name": RECORDING, "options": {"blocked": True}}, **disk(tmp_path))

        receipt = approve()
        spooled = (tmp_path / LOG_FILE).read_text()

        assert receipt.durability == "spooled"
        assert str(receipt.event_id) in spooled
        assert exporter().delivered == []

    def test_disk_spool_is_emptied_after_delivery(self, tmp_path: Path) -> None:
        configure_audit(**disk(tmp_path))

        approve()
        delivered_events()

        assert (tmp_path / LOG_FILE).read_text() == ""

    def test_spool_failure_refuses_the_event(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        configure_audit(**disk(tmp_path))

        def disk_full(self: WriteAheadLog, event: AuditEvent) -> None:
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(WriteAheadLog, "append", disk_full)

        with pytest.raises(AuditDurabilityError, match="could not be spooled"):
            approve()
        assert delivered_events() == []

    def test_exporter_boundary_delivers_before_returning(self) -> None:
        configure_audit(**inline())

        receipt = approve()

        assert receipt.durability == "delivered"
        assert [event.event_id for event in exporter().delivered] == [receipt.event_id]

    def test_exporter_boundary_retries_transient_failures_inline(self) -> None:
        configure_audit({"name": RECORDING, "options": {"fail_first": 2}}, **inline())

        approve()

        assert exporter().attempts == 3
        assert len(exporter().delivered) == 1

    def test_exporter_boundary_raises_when_required_exporter_fails(self) -> None:
        configure_audit({"name": RECORDING, "options": {"permanent": True}}, **inline())

        with pytest.raises(AuditDurabilityError, match="rejected by schema"):
            approve()

    def test_exporter_boundary_dead_letters_optional_failures(self, tmp_path: Path) -> None:
        configure_audit(
            {"name": RECORDING},
            {"name": RECORDING, "required": False, "options": {"permanent": True}},
            **inline(
                durability={"boundary": "exporter", "spool_dir": str(tmp_path)},
                retry={**FAST_RETRY, "dead_letter": "file"},
            ),
        )

        receipt = approve()

        assert receipt.durability == "delivered"
        assert str(receipt.event_id) in (tmp_path / DEAD_LETTER_FILE).read_text()

    def test_exporter_boundary_gives_up_at_the_block_timeout(self) -> None:
        configure_audit(
            {"name": RECORDING, "options": {"fail_first": 100}},
            **inline(
                block_timeout_s=0.05,
                retry={"initial_backoff_s": 0.04, "max_backoff_s": 0.04, "max_attempts": 50},
            ),
        )

        with pytest.raises(AuditDurabilityError, match="block_timeout_s"):
            approve()
        assert exporter().attempts < 5


class TestRetriesAndDeadLetters:
    def configure_with_dead_letter_file(self, spool: Path, **options: Any) -> None:
        configure_audit(
            {"name": RECORDING, "options": options},
            **disk(spool, retry={**FAST_RETRY, "dead_letter": "file"}),
        )

    def dead_letters(self, spool: Path) -> list[dict[str, Any]]:
        path = spool / DEAD_LETTER_FILE
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_transient_failures_are_retried_until_delivered(self, tmp_path: Path) -> None:
        self.configure_with_dead_letter_file(tmp_path, fail_first=2)

        approve()

        assert len(delivered_events()) == 1
        assert exporter().attempts == 3
        assert self.dead_letters(tmp_path) == []

    def test_exceptions_are_treated_as_retryable(self, tmp_path: Path) -> None:
        self.configure_with_dead_letter_file(tmp_path, fail_first=1, raises=True)

        approve()

        assert len(delivered_events()) == 1

    def test_permanent_failures_are_dead_lettered_without_retrying(self, tmp_path: Path) -> None:
        self.configure_with_dead_letter_file(tmp_path, permanent=True)

        receipt = approve()
        audit.flush(timeout_s=5)
        [dead] = self.dead_letters(tmp_path)

        assert exporter().attempts == 1
        assert dead["event"]["event_id"] == str(receipt.event_id)
        assert dead["reason"] == "recording: rejected by schema"

    def test_exhausted_retries_are_dead_lettered(self, tmp_path: Path) -> None:
        self.configure_with_dead_letter_file(tmp_path, fail_first=99)

        approve()
        audit.flush(timeout_s=5)
        [dead] = self.dead_letters(tmp_path)

        assert exporter().attempts == FAST_RETRY["max_attempts"]
        assert dead["reason"].startswith("recording: gave up: temporarily unavailable")

    def test_failure_callback_is_told_about_every_dead_letter(self, tmp_path: Path) -> None:
        failures: list[tuple[UUID, str]] = []
        configure_audit(
            {"name": RECORDING, "options": {"permanent": True}},
            **disk(
                tmp_path,
                on_delivery_failure=lambda event, reason: failures.append((event.event_id, reason)),
            ),
        )

        receipt = approve()
        audit.flush(timeout_s=5)

        assert failures == [(receipt.event_id, "recording: rejected by schema")]

    def test_only_failed_events_of_a_batch_are_retried(self, tmp_path: Path) -> None:
        class PartiallyFailing(RecordingExporter):
            def __init__(self, **options: Any) -> None:
                super().__init__(**options)
                self.entered = threading.Event()
                self.batches: list[list[str]] = []
                self._rejected_a = False

            def export(self, events: Sequence[AuditEvent]) -> ExportResult:
                self.entered.set()
                self.gate.wait()
                self.batches.append([event.resource_id for event in events])
                failures = {
                    event.event_id: Failure("busy")
                    for event in events
                    if event.resource_id == "A" and not self._rejected_a
                }
                self._rejected_a = self._rejected_a or bool(failures)
                return ExportResult(failures) if failures else ExportResult.success()

        if "partial" not in audit_exporters.names():
            audit_exporters.register("partial", PartiallyFailing)
        configure_audit({"name": "partial", "options": {"blocked": True}}, **disk(tmp_path))
        partial = exporter()
        assert isinstance(partial, PartiallyFailing)
        approve(resource_id="HOLD")
        assert partial.entered.wait(timeout=5)  # dispatcher is now parked inside export
        approve(resource_id="A")
        approve(resource_id="B")
        partial.gate.set()

        assert audit.flush(timeout_s=5)

        assert partial.batches == [["HOLD"], ["A", "B"], ["A"]]


class TestNoSilentLoss:
    def test_full_queue_refuses_instead_of_dropping(self, tmp_path: Path) -> None:
        configure_audit(
            {"name": RECORDING, "options": {"blocked": True}},
            **disk(tmp_path, queue_max_size=1, on_queue_full="raise"),
        )

        approve()

        with pytest.raises(AuditDurabilityError, match="queue is full"):
            approve()

    def test_full_queue_blocks_up_to_the_timeout(self, tmp_path: Path) -> None:
        configure_audit(
            {"name": RECORDING, "options": {"blocked": True}},
            **disk(tmp_path, queue_max_size=1, block_timeout_s=0.05),
        )

        approve()

        with pytest.raises(AuditDurabilityError, match="stayed full"):
            approve()

    def test_blocked_caller_proceeds_once_capacity_frees(self, tmp_path: Path) -> None:
        configure_audit(
            {"name": RECORDING, "options": {"blocked": True}},
            **disk(tmp_path, queue_max_size=1, block_timeout_s=5),
        )
        approve()
        threading.Timer(0.05, exporter().gate.set).start()

        approve()

        assert len(delivered_events()) == 2

    def test_every_event_is_delivered_or_dead_lettered(self, tmp_path: Path) -> None:
        failures: list[UUID] = []
        configure_audit(
            {"name": RECORDING, "options": {"fail_first": 4}},
            **disk(
                tmp_path,
                batch_size=1,
                on_delivery_failure=lambda event, _: failures.append(event.event_id),
            ),
        )

        recorded = {approve(resource_id=f"CLM-{index}").event_id for index in range(20)}
        delivered = {event.event_id for event in delivered_events()}

        assert delivered | set(failures) == recorded
        assert not delivered & set(failures)

    def test_events_stuck_in_a_hung_exporter_are_dead_lettered(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.warns(pyobservability.PyObservabilityWarning):
            pyobservability.configure(
                service_name="svc",
                logging={"output": "stderr"},
                tracing={"enabled": False},
                metrics={"enabled": False},
                shutdown_timeout_s=0.2,
                auditing={
                    "batch_size": 1,
                    "exporters": [{"name": RECORDING, "options": {"blocked": True}}],
                },
            )
        receipts = [approve(resource_id=f"CLM-{index}") for index in range(3)]

        pyobservability.shutdown()
        logged = capsys.readouterr().err

        missing = [str(r.event_id) for r in receipts if str(r.event_id) not in logged]
        assert not missing, logged

    def test_disk_events_left_at_shutdown_are_recovered_on_restart(self, tmp_path: Path) -> None:
        configure_audit(
            {"name": RECORDING, "options": {"blocked": True}},
            **disk(tmp_path),
            shutdown_timeout_s=0.1,
        )
        receipt = approve()
        pyobservability.shutdown()

        configure_audit(**disk(tmp_path))

        assert [event.event_id for event in delivered_events()] == [receipt.event_id]


class TestWriteAheadLog:
    def event(self) -> AuditEvent:
        return AuditEvent.model_validate(
            {
                "event_type": "CLAIM_APPROVED",
                "resource_type": "CLAIM",
                "resource_id": "CLM-1",
                "action": "APPROVE",
                "source": {"service": "s", "version": "1", "environment": "e", "host": "h"},
            }
        )

    def test_unacknowledged_events_are_recovered(self, tmp_path: Path) -> None:
        delivered, pending = self.event(), self.event()
        log = WriteAheadLog(tmp_path, fsync=True)
        log.open()
        log.append(delivered)
        log.append(pending)
        log.acknowledge([delivered.event_id])
        log.close()

        recovered = WriteAheadLog(tmp_path, fsync=True).open()

        assert [event.event_id for event in recovered] == [pending.event_id]

    def test_torn_final_line_is_ignored(self, tmp_path: Path) -> None:
        event = self.event()
        log = WriteAheadLog(tmp_path, fsync=True)
        log.open()
        log.append(event)
        log.close()
        with (tmp_path / LOG_FILE).open("a") as file:
            file.write('{"append": {"event_ty')

        recovered = WriteAheadLog(tmp_path, fsync=True).open()

        assert [entry.event_id for entry in recovered] == [event.event_id]

    def test_spool_directory_has_a_single_owner(self, tmp_path: Path) -> None:
        WriteAheadLog(tmp_path, fsync=True).open()

        with pytest.raises(ConfigurationError, match="in use by another process"):
            WriteAheadLog(tmp_path, fsync=True).open()

    def test_spool_files_are_private(self, tmp_path: Path) -> None:
        log = WriteAheadLog(tmp_path / "spool", fsync=True)
        log.open()

        assert (tmp_path / "spool" / LOG_FILE).stat().st_mode & 0o077 == 0


class TestRetrySchedule:
    def test_backoff_grows_exponentially_up_to_the_cap(self) -> None:
        schedule = RetrySchedule(
            RetryConfig(initial_backoff_s=1, max_backoff_s=5, multiplier=2, jitter=0),
        )

        assert [schedule.delay_after(attempt) for attempt in range(1, 6)] == [1, 2, 4, 5, 5]

    @pytest.mark.parametrize(("random_value", "expected"), [(0.0, 0.8), (0.5, 1.0), (1.0, 1.2)])
    def test_jitter_spreads_delays_symmetrically(
        self, random_value: float, expected: float
    ) -> None:
        schedule = RetrySchedule(RetryConfig(initial_backoff_s=1, jitter=0.2), lambda: random_value)

        assert schedule.delay_after(1) == pytest.approx(expected)


class TestConfigurationRules:
    def test_exporter_boundary_requires_inline_delivery(self) -> None:
        with pytest.raises(ConfigurationError, match="must be used together"):
            configure_audit(durability={"boundary": "exporter"})

    def test_file_dead_letters_need_a_spool_directory(self) -> None:
        with pytest.raises(ConfigurationError, match="requires durability.spool_dir"):
            configure_audit(retry={"dead_letter": "file"})

    def test_unknown_audit_exporter_is_reported(self) -> None:
        with (
            pytest.warns(pyobservability.PyObservabilityWarning),
            pytest.raises(ConfigurationError, match="unknown audit exporter"),
        ):
            configure_audit({"name": "carrier-pigeon"})


class TestBuiltInExporters:
    def event(self) -> AuditEvent:
        return TestWriteAheadLog().event()

    def test_console_exporter_writes_json_lines(self) -> None:
        stream = io.StringIO()

        result = ConsoleAuditExporter(stream).export([self.event(), self.event()])
        lines = stream.getvalue().splitlines()

        assert result.failures == {}
        assert [json.loads(line)["event_type"] for line in lines] == ["CLAIM_APPROVED"] * 2

    def test_file_exporter_appends_to_a_private_file(self, tmp_path: Path) -> None:
        path = tmp_path / "audit" / "events.jsonl"
        exporter = JsonLinesAuditExporter(path)
        event = self.event()

        exporter.export([event])
        exporter.export([event])
        exporter.shutdown()

        assert [json.loads(line)["event_id"] for line in path.read_text().splitlines()] == [
            str(event.event_id)
        ] * 2
        assert path.stat().st_mode & 0o077 == 0

    def test_file_exporter_reports_write_errors_as_retryable(self, tmp_path: Path) -> None:
        exporter = JsonLinesAuditExporter(tmp_path / "events.jsonl")
        exporter.shutdown()
        event = self.event()

        result = exporter.export([event])

        assert result.failures[event.event_id].retryable
        assert "cannot write" in result.failures[event.event_id].reason

    def test_file_exporter_is_selectable_by_name(self, tmp_path: Path) -> None:
        path = tmp_path / "events.jsonl"
        configure_audit({"name": "file", "options": {"path": str(path)}}, **inline())

        receipt = approve()

        assert str(receipt.event_id) in path.read_text()


def test_fallback_uuid7_is_version_7_and_time_ordered(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delattr("pyobservability.audit.schemas.uuid.uuid7", raising=False)

    first = uuid7()
    time.sleep(0.002)
    second = uuid7()

    assert (first.version, first.variant) == (7, RFC_4122)
    assert first < second


class TestForkSafety:
    def test_a_forked_child_cannot_record_into_a_dead_dispatcher(
        self, quiet_memory_audit: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("pyobservability.audit.service.os.getpid", lambda: 4_000_000)

        with pytest.raises(AuditDurabilityError, match="configure\\(\\) in each worker process"):
            approve()

    def test_the_starting_process_keeps_recording(self, quiet_memory_audit: None) -> None:
        approve()

        assert len(delivered_events()) == 1


@pytest.mark.parametrize("name", ["public\n", "Public", "1schema", "a-b", "x" * 64])
def test_unsafe_schema_names_are_rejected(name: str) -> None:
    with pytest.raises(pyobservability.ConfigurationError, match="invalid PostgreSQL schema"):
        validated_identifier(name)
