from collections.abc import Iterator, Sequence
from importlib.resources import files
from typing import Any
from uuid import UUID

from pydantic import SecretStr

from pyobservability.audit.integrity import (
    GLOBAL_CHAIN,
    ChainLink,
    ChainSigner,
    StoredEvent,
    VerifyResult,
    chain_key,
)
from pyobservability.audit.models import ExportResult, Failure
from pyobservability.audit.pg import resolve_dsn, validated_identifier
from pyobservability.audit.schemas import AuditEvent
from pyobservability.exceptions import MissingExtraError

try:
    import psycopg
    from psycopg import sql
    from psycopg.types.json import Jsonb
    from psycopg_pool import ConnectionPool
except ImportError as error:
    raise MissingExtraError("postgres", "the PostgreSQL audit exporter") from error

SCHEMA_FILE = "postgres.sql"
VERIFY_FETCH_SIZE = 1000


class PostgresAuditExporter:
    """Append-only, hash-chained audit storage; idempotent on `event_id`."""

    name = "postgres"

    def __init__(
        self,
        dsn: str | SecretStr | None = None,
        *,
        dsn_env: str | None = None,
        schema: str = "public",
        create_schema: bool = True,
        pool_max_size: int = 4,
    ) -> None:
        self._schema = sql.Identifier(validated_identifier(schema))
        self._signer = ChainSigner({}, None)
        self._pool = ConnectionPool(
            resolve_dsn(dsn, dsn_env, owner="the postgres audit exporter"),
            min_size=1,
            max_size=pool_max_size,
            open=True,
        )
        if create_schema:
            self.install_schema()

    def use_signer(self, signer: ChainSigner) -> None:
        self._signer = signer

    def install_schema(self) -> None:
        template = files("pyobservability.audit").joinpath("sql", SCHEMA_FILE).read_text()
        with self._pool.connection() as connection:
            connection.execute(sql.SQL(template).format(schema=self._schema))

    def export(self, events: Sequence[AuditEvent]) -> ExportResult:
        try:
            self._append_all(events)
        except psycopg.DataError:
            return self._export_one_by_one(events)
        except psycopg.Error as error:
            return ExportResult.all_failed(events, f"PostgreSQL unavailable: {error}")
        return ExportResult.success()

    def _export_one_by_one(self, events: Sequence[AuditEvent]) -> ExportResult:
        """Isolates the rows PostgreSQL rejects so the rest of the batch is still stored."""
        failures: dict[UUID, Failure] = {}
        for event in events:
            try:
                self._append_all([event])
            except psycopg.DataError as error:
                failures[event.event_id] = Failure(f"rejected by PostgreSQL: {error}", False)
            except psycopg.Error as error:
                failures[event.event_id] = Failure(f"PostgreSQL unavailable: {error}")
        return ExportResult(failures)

    def _append_all(self, events: Sequence[AuditEvent]) -> None:
        with self._pool.connection() as connection, connection.transaction():
            for event in events:
                self._append(connection, event)

    def verify(self, tenant_id: str | None) -> VerifyResult:
        with self._pool.connection() as connection:
            return self._signer.verify(self._chain(connection, tenant_id))

    def shutdown(self) -> None:
        self._pool.close()

    def _append(self, connection: Any, event: AuditEvent) -> None:
        if self._already_stored(connection, event):
            return
        key = chain_key(event)
        link = self._signer.link(event, self._locked_head(connection, key))
        connection.execute(
            sql.SQL(
                "INSERT INTO {schema}.audit_events (event_id, chain_key, seq, prev_hash, hash,"
                " key_id, tenant_id, event_type, category, outcome, actor_id, resource_type,"
                " resource_id, action, occurred_at, recorded_at, payload)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
            ).format(schema=self._schema),
            (
                event.event_id,
                key,
                link.seq,
                link.prev_hash,
                link.hash,
                link.key_id,
                event.tenant_id,
                event.event_type,
                event.category,
                event.outcome,
                event.actor_id,
                event.resource_type,
                event.resource_id,
                event.action,
                event.occurred_at,
                event.recorded_at,
                Jsonb(event.model_dump(mode="json")),
            ),
        )
        connection.execute(
            sql.SQL(
                "UPDATE {schema}.audit_chain_heads SET seq = %s, hash = %s WHERE chain_key = %s"
            ).format(schema=self._schema),
            (link.seq, link.hash, key),
        )

    def _already_stored(self, connection: Any, event: AuditEvent) -> bool:
        query = sql.SQL("SELECT 1 FROM {schema}.audit_events WHERE event_id = %s").format(
            schema=self._schema
        )
        return connection.execute(query, (event.event_id,)).fetchone() is not None

    def _locked_head(self, connection: Any, key: str) -> ChainLink | None:
        """The row lock serialises appends to one chain across every process and host."""
        connection.execute(
            sql.SQL(
                "INSERT INTO {schema}.audit_chain_heads (chain_key, seq, hash)"
                " VALUES (%s, 0, '') ON CONFLICT (chain_key) DO NOTHING"
            ).format(schema=self._schema),
            (key,),
        )
        seq, head_hash = connection.execute(
            sql.SQL(
                "SELECT seq, hash FROM {schema}.audit_chain_heads WHERE chain_key = %s FOR UPDATE"
            ).format(schema=self._schema),
            (key,),
        ).fetchone()
        return None if seq == 0 else ChainLink(seq, "", head_hash, None)

    def _chain(self, connection: Any, tenant_id: str | None) -> Iterator[StoredEvent]:
        key = tenant_id or GLOBAL_CHAIN
        with connection.cursor(name="pyobservability_verify") as cursor:
            cursor.itersize = VERIFY_FETCH_SIZE
            cursor.execute(
                sql.SQL(
                    "SELECT seq, prev_hash, hash, key_id, payload FROM {schema}.audit_events"
                    " WHERE chain_key = %s ORDER BY seq"
                ).format(schema=self._schema),
                (key,),
            )
            for seq, prev_hash, digest, key_id, payload in cursor:
                yield StoredEvent(
                    ChainLink(seq, prev_hash, digest, key_id), AuditEvent.model_validate(payload)
                )
