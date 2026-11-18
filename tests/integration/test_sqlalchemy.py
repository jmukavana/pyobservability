import json
from collections.abc import Iterator
from typing import Any

import pytest
from opentelemetry.trace import SpanKind, StatusCode
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import OperationalError

from pyobservability import get_tracer
from pyobservability.integrations.sqlalchemy import instrument_engine


@pytest.fixture
def engine() -> Iterator[Engine]:
    database = create_engine("sqlite://")
    with database.begin() as connection:
        connection.execute(text("CREATE TABLE claims (id INTEGER, holder_email TEXT)"))
    yield instrument_engine(database)
    database.dispose()


def client_spans(spans: list[Any]) -> list[Any]:
    return [span for span in spans if span.kind is SpanKind.CLIENT]


def test_statements_become_client_spans(engine: Engine, telemetry: Any) -> None:
    telemetry.configure()

    with engine.connect() as connection:
        connection.execute(text("SELECT id FROM claims WHERE id = :id"), {"id": 7})
    [span] = client_spans(telemetry.spans())

    assert span.name == "SELECT"
    assert span.attributes["db.system.name"] == "sqlite"
    assert span.attributes["db.operation.name"] == "SELECT"
    assert span.attributes["db.query.text"] == "SELECT id FROM claims WHERE id = ?"


def test_bound_parameters_are_never_captured(engine: Engine, telemetry: Any) -> None:
    telemetry.configure()

    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO claims VALUES (:id, :email)"), {"id": 1, "email": "jane@example.com"}
        )
    [span] = client_spans(telemetry.spans())

    assert "jane@example.com" not in json.dumps(dict(span.attributes))


def test_statement_spans_nest_under_the_current_span(engine: Engine, telemetry: Any) -> None:
    telemetry.configure()

    with (
        get_tracer(__name__).start_as_current_span("approve claim") as parent,
        engine.connect() as connection,
    ):
        connection.execute(text("SELECT 1"))
    [span] = client_spans(telemetry.spans())

    assert span.parent.span_id == parent.get_span_context().span_id


def test_failed_statements_are_recorded(engine: Engine, telemetry: Any) -> None:
    telemetry.configure()

    with pytest.raises(OperationalError), engine.connect() as connection:
        connection.execute(text("SELECT * FROM no_such_table"))
    [span] = client_spans(telemetry.spans())

    assert span.status.status_code is StatusCode.ERROR
    assert span.attributes["error.type"] == "OperationalError"
    assert [event.name for event in span.events] == ["exception"]


def test_duration_metric_has_bounded_labels(engine: Engine, telemetry: Any) -> None:
    telemetry.configure()

    with engine.connect() as connection:
        connection.execute(text("SELECT id FROM claims"))
        connection.execute(text("SELECT id FROM claims WHERE id = 3"))
    [point] = telemetry.points("db.client.operation.duration")

    assert point.count == 2
    assert point.attributes == {"db.system.name": "sqlite", "db.operation.name": "SELECT"}


def test_instrumenting_twice_does_not_duplicate_spans(engine: Engine, telemetry: Any) -> None:
    telemetry.configure()
    instrument_engine(engine)

    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))

    assert len(client_spans(telemetry.spans())) == 1


def test_slow_queries_are_logged(telemetry: Any, capsys: pytest.CaptureFixture[str]) -> None:
    telemetry.configure(logging={"output": "stderr"})
    engine = instrument_engine(create_engine("sqlite://"), slow_query_threshold_s=0.0)

    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))
    engine.dispose()
    slow = [
        json.loads(line)
        for line in capsys.readouterr().err.splitlines()
        if "slow database query" in line
    ]

    assert slow[0]["operation"] == "SELECT"
    assert slow[0]["level"] == "WARNING"
