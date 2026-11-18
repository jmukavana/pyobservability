import shutil
import threading
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg import sql

import pyobservability
from pyobservability import audit
from pyobservability.audit import AuditEvent
from pyobservability.audit.exporters.postgres import PostgresAuditExporter
from pyobservability.audit.integrity import ChainSigner

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(shutil.which("initdb") is None, reason="PostgreSQL server binaries"),
]

SOURCE = {"service": "claims", "version": "1", "environment": "test", "host": "h"}
KEYS = {"2026-11": "k" * 32}


@pytest.fixture
def schema() -> str:
    return f"audit_{uuid.uuid4().hex[:10]}"


def query(dsn: str, template: str, schema: str) -> list[tuple[Any, ...]]:
    """`{schema}` in the template is quoted as an identifier, never interpolated as text."""
    statement = sql.SQL(template).format(schema=sql.Identifier(schema))
    with psycopg.connect(dsn, autocommit=True) as connection:
        cursor = connection.execute(statement)
        return cursor.fetchall() if cursor.description else []


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


@pytest.fixture
def exporter(postgres_dsn: str, schema: str) -> Iterator[PostgresAuditExporter]:
    store = PostgresAuditExporter(postgres_dsn, schema=schema)
    store.use_signer(ChainSigner({key: value.encode() for key, value in KEYS.items()}, "2026-11"))
    yield store
    store.shutdown()


def configure_postgres_audit(dsn: str, schema: str) -> None:
    pyobservability.configure(
        service_name="claims",
        logging={"enabled": False},
        tracing={"enabled": False},
        metrics={"enabled": False},
        auditing={
            "delivery": "sync",
            "durability": {"boundary": "exporter"},
            "exporters": [{"name": "postgres", "options": {"dsn": dsn, "schema": schema}}],
            "integrity": {"keys": KEYS, "active_key_id": "2026-11"},
        },
    )


class TestStorage:
    def test_recorded_events_are_stored_and_verifiable(
        self, postgres_dsn: str, schema: str
    ) -> None:
        configure_postgres_audit(postgres_dsn, schema)
        for index in range(3):
            audit.record(
                event_type="CLAIM_APPROVED",
                resource_type="CLAIM",
                resource_id=f"CLM-{index}",
                action="APPROVE",
                tenant_id="tenant-1",
            )

        result = audit.verify("tenant-1")
        rows = query(
            postgres_dsn, "SELECT seq, key_id, hash FROM {schema}.audit_events ORDER BY seq", schema
        )

        assert result.ok
        assert result.checked == 3
        assert [row[0] for row in rows] == [1, 2, 3, 4]
        assert {row[1] for row in rows} == {"2026-11"}
        assert all(row[2].startswith("hmac-sha256:") for row in rows)

    def test_verification_is_itself_audited(self, postgres_dsn: str, schema: str) -> None:
        configure_postgres_audit(postgres_dsn, schema)

        audit.verify("tenant-1")
        [(category, outcome, resource_id)] = query(
            postgres_dsn,
            "SELECT category, outcome, resource_id FROM {schema}.audit_events"
            " WHERE event_type = 'AUDIT_CHAIN_VERIFIED'",
            schema,
        )

        assert (category, outcome, resource_id) == ("access", "success", "tenant-1")

    def test_redelivered_events_are_stored_once(
        self, exporter: PostgresAuditExporter, postgres_dsn: str, schema: str
    ) -> None:
        duplicate = event()

        exporter.export([duplicate])
        exporter.export([duplicate, duplicate])

        assert query(postgres_dsn, "SELECT count(*) FROM {schema}.audit_events", schema) == [(1,)]
        assert exporter.verify("tenant-1").ok

    def test_tenants_have_independent_chains(
        self, exporter: PostgresAuditExporter, postgres_dsn: str, schema: str
    ) -> None:
        exporter.export([event("A", "tenant-a"), event("B", "tenant-b"), event("C", "tenant-a")])

        rows = query(
            postgres_dsn,
            "SELECT chain_key, seq FROM {schema}.audit_events ORDER BY chain_key, seq",
            schema,
        )

        assert rows == [("tenant-a", 1), ("tenant-a", 2), ("tenant-b", 1)]

    def test_rejected_rows_do_not_block_the_rest_of_the_batch(
        self, exporter: PostgresAuditExporter, postgres_dsn: str, schema: str
    ) -> None:
        good, bad, also_good = event("ok-1"), event("nul\x00byte"), event("ok-2")

        result = exporter.export([good, bad, also_good])

        assert list(result.failures) == [bad.event_id]
        assert not result.failures[bad.event_id].retryable
        assert query(postgres_dsn, "SELECT count(*) FROM {schema}.audit_events", schema) == [(2,)]


class TestTamperEvidence:
    @pytest.mark.parametrize(
        "statement",
        [
            "UPDATE {schema}.audit_events SET actor_id = 'x'",
            "DELETE FROM {schema}.audit_events",
            "TRUNCATE {schema}.audit_events",
        ],
    )
    def test_audit_table_is_append_only(
        self, exporter: PostgresAuditExporter, postgres_dsn: str, schema: str, statement: str
    ) -> None:
        exporter.export([event()])

        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            query(postgres_dsn, statement, schema)

    def test_tampering_by_a_database_administrator_is_detected(
        self, exporter: PostgresAuditExporter, postgres_dsn: str, schema: str
    ) -> None:
        exporter.export([event(f"CLM-{index}") for index in range(4)])
        query(
            postgres_dsn,
            "ALTER TABLE {schema}.audit_events DISABLE TRIGGER audit_events_append_only;"
            " UPDATE {schema}.audit_events"
            " SET payload = jsonb_set(payload, '{{action}}', '\"REJECT\"') WHERE seq = 3",
            schema,
        )

        result = exporter.verify("tenant-1")

        assert not result.ok
        assert result.first_bad_seq == 3

    def test_verifying_with_the_wrong_key_fails(
        self, exporter: PostgresAuditExporter, postgres_dsn: str, schema: str
    ) -> None:
        exporter.export([event()])
        exporter.use_signer(ChainSigner({"2026-11": b"not-the-key"}, "2026-11"))

        assert exporter.verify("tenant-1").problem == "record content does not match its hash"


def test_concurrent_writers_keep_one_gap_free_chain(postgres_dsn: str, schema: str) -> None:
    signer = ChainSigner({}, None)
    writers = [PostgresAuditExporter(postgres_dsn, schema=schema) for _ in range(3)]
    for writer in writers:
        writer.use_signer(signer)

    def write(writer: PostgresAuditExporter, offset: int) -> None:
        for index in range(10):
            assert not writer.export([event(f"W{offset}-{index}")]).failures

    threads = [
        threading.Thread(target=write, args=(writer, offset))
        for offset, writer in enumerate(writers)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    result = writers[0].verify("tenant-1")
    for writer in writers:
        writer.shutdown()

    assert result.ok
    assert result.checked == 30


def test_dsn_can_come_from_an_environment_variable(
    postgres_dsn: str, schema: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("AUDIT_DATABASE_URL", postgres_dsn)

    store = PostgresAuditExporter(dsn_env="AUDIT_DATABASE_URL", schema=schema)
    store.shutdown()


def test_missing_dsn_is_a_configuration_error() -> None:
    with pytest.raises(pyobservability.ConfigurationError, match="needs `dsn`"):
        PostgresAuditExporter(dsn_env="UNSET_AUDIT_DATABASE_URL")


def test_schema_names_are_validated() -> None:
    with pytest.raises(pyobservability.ConfigurationError, match="invalid PostgreSQL schema"):
        PostgresAuditExporter("host=unused", schema="public; DROP TABLE x")
