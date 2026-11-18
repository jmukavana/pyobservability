"""Transactional outbox: audit events commit or roll back with the caller's own transaction."""

import logging
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from functools import singledispatch
from importlib.resources import files
from uuid import UUID

from pydantic import ValidationError

from pyobservability.audit.delivery import Delivery, DeliveryInterrupted
from pyobservability.audit.outcomes import DeliveryOutcomes
from pyobservability.audit.pg import resolve_dsn, validated_identifier
from pyobservability.audit.schemas import AuditEvent
from pyobservability.config import OutboxConfig
from pyobservability.exceptions import AuditDurabilityError, MissingExtraError

try:
    import psycopg
    from psycopg import sql
    from psycopg_pool import ConnectionPool
except ImportError as error:
    raise MissingExtraError("postgres", "the audit outbox") from error

SCHEMA_FILE = "outbox.sql"
OUTBOX_TABLE = "audit_outbox"
EMPTY_POLL_INTERVAL_S = 0.02

_logger = logging.getLogger("pyobservability.audit")


@dataclass(frozen=True, slots=True)
class OutboxRow:
    schema: str
    event_id: UUID
    payload: str

    @property
    def named_insert(self) -> str:
        """For drivers taking `:name` parameters; `schema` was validated as an identifier."""
        table = f'"{self.schema}"."{OUTBOX_TABLE}"'
        columns = "(event_id, payload) VALUES (:event_id, CAST(:payload AS jsonb))"
        return f"INSERT INTO {table} {columns} ON CONFLICT (event_id) DO NOTHING"  # noqa: S608


@singledispatch
def write_outbox_row(transaction: object, row: OutboxRow) -> None:
    """Writes through the caller's transaction; integrations register their transaction types."""
    raise TypeError(
        f"cannot write audit events through {type(transaction).__name__}; pass a psycopg "
        "connection, or a SQLAlchemy Session/Connection with the sqlalchemy integration imported"
    )


@write_outbox_row.register
def _(transaction: psycopg.Connection, row: OutboxRow) -> None:
    statement = sql.SQL(
        "INSERT INTO {}.{} (event_id, payload) VALUES (%s, %s::jsonb)"
        " ON CONFLICT (event_id) DO NOTHING"
    ).format(sql.Identifier(row.schema), sql.Identifier(OUTBOX_TABLE))
    transaction.execute(statement, (row.event_id, row.payload))


class Outbox:
    def __init__(self, config: OutboxConfig) -> None:
        self.config = config
        self._schema_name = validated_identifier(config.schema_name)
        self._schema = sql.Identifier(self._schema_name)
        self._pool = ConnectionPool(
            resolve_dsn(config.dsn, config.dsn_env, owner="the audit outbox"),
            min_size=1,
            max_size=4,
            open=True,
        )
        if config.create_schema:
            self._install_schema()

    def write(self, event: AuditEvent, transaction: object | None) -> None:
        row = OutboxRow(self._schema_name, event.event_id, event.model_dump_json())
        try:
            if transaction is not None:
                write_outbox_row(transaction, row)
                return
            with self._pool.connection() as connection:
                write_outbox_row(connection, row)
        except psycopg.Error as error:
            raise AuditDurabilityError(f"audit event could not be written: {error}") from error

    def relay(
        self, delivery: Delivery, outcomes: DeliveryOutcomes, batch_size: int
    ) -> "OutboxRelay":
        return OutboxRelay(self, delivery, outcomes, self.config, batch_size)

    def claim(self, limit: int, lease_s: float) -> list[tuple[int, AuditEvent]]:
        """Leases rows no other relay holds; a crashed relay's lease simply expires."""
        statement = sql.SQL(
            "UPDATE {table} SET lease_until = now() + make_interval(secs => %s),"
            " attempts = attempts + 1 WHERE id IN ("
            " SELECT id FROM {table} WHERE lease_until IS NULL OR lease_until < now()"
            " ORDER BY id LIMIT %s FOR UPDATE SKIP LOCKED) RETURNING id, payload"
        ).format(table=self._qualified())
        with self._pool.connection() as connection:
            rows = connection.execute(statement, (lease_s, limit)).fetchall()
        return [claimed for row_id, payload in rows if (claimed := _parsed(row_id, payload))]

    def complete(self, row_ids: Sequence[int]) -> None:
        self._execute("DELETE FROM {table} WHERE id = ANY(%s)", row_ids)

    def release(self, row_ids: Sequence[int]) -> None:
        self._execute("UPDATE {table} SET lease_until = NULL WHERE id = ANY(%s)", row_ids)

    def pending(self) -> int:
        statement = sql.SQL("SELECT count(*) FROM {table}").format(table=self._qualified())
        with self._pool.connection() as connection:
            row = connection.execute(statement).fetchone()
        return int(row[0]) if row else 0

    def close(self) -> None:
        self._pool.close()

    def _execute(self, template: str, row_ids: Sequence[int]) -> None:
        statement = sql.SQL(template).format(table=self._qualified())
        with self._pool.connection() as connection:
            connection.execute(statement, (list(row_ids),))

    def _qualified(self) -> sql.Composed:
        return sql.SQL("{}.{}").format(self._schema, sql.Identifier(OUTBOX_TABLE))

    def _install_schema(self) -> None:
        template = files("pyobservability.audit").joinpath("sql", SCHEMA_FILE).read_text()
        with self._pool.connection() as connection:
            connection.execute(sql.SQL(template).format(schema=self._schema))


def _parsed(row_id: int, payload: object) -> tuple[int, AuditEvent] | None:
    try:
        return row_id, AuditEvent.model_validate(payload)
    except ValidationError:
        _logger.exception("audit outbox row %s holds an invalid event; leaving it in place", row_id)
        return None


class OutboxRelay:
    """Delivers committed outbox rows in the background, then deletes them."""

    def __init__(
        self,
        outbox: Outbox,
        delivery: Delivery,
        outcomes: DeliveryOutcomes,
        config: OutboxConfig,
        batch_size: int,
    ) -> None:
        self._outbox = outbox
        self._delivery = delivery
        self._outcomes = outcomes
        self._config = config
        self._batch_size = batch_size
        self._stop = threading.Event()
        self._work_arrived = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="pyobservability-outbox", daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def nudge(self) -> None:
        self._work_arrived.set()

    def wait_until_empty(self, timeout_s: float) -> bool:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            self.nudge()
            if self._outbox.pending() == 0:
                return True
            time.sleep(EMPTY_POLL_INTERVAL_S)
        return False

    def shutdown(self, timeout_s: float) -> None:
        self._stop.set()
        self._work_arrived.set()
        self._thread.join(timeout_s)
        self._outbox.close()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                claimed = self._outbox.claim(self._batch_size, self._config.lease_s)
            except psycopg.Error:
                _logger.exception("audit outbox is unreachable; retrying")
                claimed = []
            if claimed:
                self._process(claimed)
                continue
            self._work_arrived.wait(self._config.poll_interval_s)
            self._work_arrived.clear()

    def _process(self, claimed: list[tuple[int, AuditEvent]]) -> None:
        row_ids = [row_id for row_id, _ in claimed]
        events = [event for _, event in claimed]
        try:
            undelivered = self._delivery.deliver(events, self._stop.wait)
        except DeliveryInterrupted:
            self._outbox.release(row_ids)
            return
        self._outcomes.settle(events, undelivered)
        self._outbox.complete(row_ids)
