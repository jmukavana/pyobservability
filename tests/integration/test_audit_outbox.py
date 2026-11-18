import threading
import uuid
from collections.abc import Iterator, Sequence
from typing import Any

import psycopg
import pytest
from pydantic import SecretStr
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

import pyobservability
import pyobservability.integrations.sqlalchemy  # noqa: F401 - registers Session/Connection writers
from pyobservability import ConfigurationError, audit
from pyobservability.audit import AuditEvent, ExportResult, audit_exporters
from pyobservability.audit.outbox import Outbox
from pyobservability.config import OutboxConfig

pytestmark = pytest.mark.integration

COLLECTOR = "outbox-collector"


class Collector:
    name = COLLECTOR
    received: list[AuditEvent] = []
    lock = threading.Lock()

    def __init__(self, refuse: bool = False) -> None:
        self._refuse = refuse

    def export(self, events: Sequence[AuditEvent]) -> ExportResult:
        if self._refuse:
            return ExportResult.all_failed(events, "schema rejected", retryable=False)
        with Collector.lock:
            Collector.received.extend(events)
        return ExportResult.success()

    def shutdown(self) -> None:
        pass


if COLLECTOR not in audit_exporters.names():
    audit_exporters.register(COLLECTOR, Collector)


@pytest.fixture
def schema() -> str:
    return f"outbox_{uuid.uuid4().hex[:10]}"


@pytest.fixture(autouse=True)
def empty_collector() -> Iterator[None]:
    Collector.received = []
    yield


def configure_outbox(
    dsn: str, schema: str, *, on_delivery_failure: Any = None, **collector_options: Any
) -> None:
    pyobservability.configure(
        service_name="claims",
        logging={"enabled": False},
        tracing={"enabled": False},
        metrics={"enabled": False},
        auditing={
            "exporters": [{"name": COLLECTOR, "options": collector_options}],
            "retry": {"initial_backoff_s": 0.001, "max_backoff_s": 0.002, "max_attempts": 2},
            "on_delivery_failure": on_delivery_failure,
            "durability": {
                "boundary": "outbox",
                "outbox": {"dsn": dsn, "schema_name": schema, "poll_interval_s": 0.05},
            },
        },
    )


def approve(**extra: Any) -> audit.AuditReceipt:
    return audit.record(
        event_type="CLAIM_APPROVED",
        resource_type="CLAIM",
        resource_id="CLM-1",
        action="APPROVE",
        **extra,
    )


def received_ids() -> list[uuid.UUID]:
    assert audit.flush(timeout_s=10)
    return [event.event_id for event in Collector.received]


INSERT_CLAIM = "INSERT INTO claims_business (id, status) VALUES (%s, 'APPROVED')"


def business_table(dsn: str) -> None:
    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS claims_business (id text PRIMARY KEY, status text)"
        )


class TestTransactionalWrites:
    def test_event_commits_with_a_psycopg_transaction(self, postgres_dsn: str, schema: str) -> None:
        configure_outbox(postgres_dsn, schema)
        business_table(postgres_dsn)

        with psycopg.connect(postgres_dsn) as connection:
            connection.execute(INSERT_CLAIM, (uuid.uuid4().hex,))
            receipt = approve(transaction=connection)

        assert receipt.durability == "stored"
        assert received_ids() == [receipt.event_id]

    def test_rolled_back_transaction_leaves_no_audit_event(
        self, postgres_dsn: str, schema: str
    ) -> None:
        configure_outbox(postgres_dsn, schema)

        with psycopg.connect(postgres_dsn) as connection:
            approve(transaction=connection)
            connection.rollback()

        assert received_ids() == []

    def test_sqlalchemy_session_commit_and_rollback(self, postgres_dsn: str, schema: str) -> None:
        configure_outbox(postgres_dsn, schema)
        business_table(postgres_dsn)
        engine = create_engine(f"postgresql+psycopg:///?{_as_query(postgres_dsn)}")
        try:
            with Session(engine) as session, session.begin():
                session.execute(
                    text("INSERT INTO claims_business (id, status) VALUES (:id, 'APPROVED')"),
                    {"id": uuid.uuid4().hex},
                )
                committed = approve(transaction=session)
            with Session(engine) as session:
                approve(transaction=session)
                session.rollback()
        finally:
            engine.dispose()

        assert received_ids() == [committed.event_id]

    def test_without_a_transaction_the_outbox_commits_on_its_own(
        self, postgres_dsn: str, schema: str
    ) -> None:
        configure_outbox(postgres_dsn, schema)

        receipt = approve()

        assert received_ids() == [receipt.event_id]

    def test_retried_records_do_not_break_the_callers_transaction(
        self, postgres_dsn: str, schema: str
    ) -> None:
        configure_outbox(postgres_dsn, schema)
        event_id = uuid.uuid4()

        with psycopg.connect(postgres_dsn) as connection:
            approve(transaction=connection, event_id=event_id)
            approve(transaction=connection, event_id=event_id)

        assert received_ids() == [event_id]

    def test_unsupported_transaction_types_are_rejected(
        self, postgres_dsn: str, schema: str
    ) -> None:
        configure_outbox(postgres_dsn, schema)

        with pytest.raises(TypeError, match="cannot write audit events through dict"):
            approve(transaction={})

    def test_transaction_needs_the_outbox_boundary(self) -> None:
        with pytest.warns(pyobservability.PyObservabilityWarning):
            pyobservability.configure(
                service_name="svc",
                logging={"enabled": False},
                tracing={"enabled": False},
                metrics={"enabled": False},
                auditing={"exporters": [{"name": COLLECTOR}]},
            )

        with pytest.raises(ConfigurationError, match="boundary='outbox'"):
            approve(transaction=object())


class TestRelay:
    def test_undeliverable_events_are_dead_lettered_and_removed(
        self, postgres_dsn: str, schema: str
    ) -> None:
        failures: list[uuid.UUID] = []
        configure_outbox(
            postgres_dsn,
            schema,
            refuse=True,
            on_delivery_failure=lambda event, _: failures.append(event.event_id),
        )

        receipt = approve()

        assert audit.flush(timeout_s=10)
        assert Collector.received == []
        assert failures == [receipt.event_id]

    def test_rows_leased_by_a_crashed_relay_are_reclaimed(
        self, postgres_dsn: str, schema: str
    ) -> None:
        config = OutboxConfig(dsn=SecretStr(postgres_dsn), schema_name=schema)
        first, second = Outbox(config), Outbox(config)
        event = _event()
        first.write(event, None)

        [(row_id, claimed)] = first.claim(limit=10, lease_s=0.05)
        immediately = second.claim(limit=10, lease_s=60)
        threading.Event().wait(0.1)
        reclaimed = second.claim(limit=10, lease_s=60)
        first.close()
        second.close()

        assert claimed.event_id == event.event_id
        assert immediately == []
        assert [row for row, _ in reclaimed] == [row_id]

    def test_concurrent_relays_never_claim_the_same_row(
        self, postgres_dsn: str, schema: str
    ) -> None:
        config = OutboxConfig(dsn=SecretStr(postgres_dsn), schema_name=schema)
        relays = [Outbox(config) for _ in range(3)]
        for _ in range(30):
            relays[0].write(_event(), None)
        claimed: list[int] = []
        lock = threading.Lock()

        def claim_all(outbox: Outbox) -> None:
            while rows := outbox.claim(limit=4, lease_s=60):
                with lock:
                    claimed.extend(row for row, _ in rows)

        threads = [threading.Thread(target=claim_all, args=(outbox,)) for outbox in relays]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        for outbox in relays:
            outbox.close()

        assert len(claimed) == 30
        assert len(set(claimed)) == 30


def _event() -> AuditEvent:
    return AuditEvent.model_validate(
        {
            "event_type": "CLAIM_APPROVED",
            "resource_type": "CLAIM",
            "resource_id": "CLM-1",
            "action": "APPROVE",
            "source": {"service": "s", "version": "1", "environment": "e", "host": "h"},
        }
    )


def _as_query(dsn: str) -> str:
    return "&".join(dsn.split())
