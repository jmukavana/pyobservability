import getpass
import os
import shutil
import socket
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import structlog
from opentelemetry.sdk.metrics.export import MetricExporter, MetricExportResult, MetricsData
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import pyobservability
from pyobservability.exporters import metric_exporters, span_exporters

ENVIRONMENT_PREFIXES = ("PYOBS_", "OTEL_")
TEST_EXPORTER = "test-memory"


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in list(os.environ):
        if name.startswith(ENVIRONMENT_PREFIXES):
            monkeypatch.delenv(name)
    monkeypatch.chdir(tmp_path)


@pytest.fixture(autouse=True)
def no_active_runtime() -> Iterator[None]:
    yield
    pyobservability.shutdown()
    pyobservability.context.clear()
    structlog.reset_defaults()


@pytest.fixture
def settings() -> dict[str, Any]:
    return {"service_name": "test-service", "auditing": {"enabled": False}}


class CollectedMetrics(MetricExporter):
    def __init__(self) -> None:
        super().__init__()
        self.exports: list[MetricsData] = []

    def export(
        self, metrics_data: MetricsData, timeout_millis: float = 10_000, **_: Any
    ) -> MetricExportResult:
        self.exports.append(metrics_data)
        return MetricExportResult.SUCCESS

    def force_flush(self, timeout_millis: float = 10_000) -> bool:
        return True

    def shutdown(self, timeout_millis: float = 30_000, **_: Any) -> None:
        pass

    def points(self, name: str) -> list[Any]:
        return [
            point
            for data in self.exports
            for resource_metrics in data.resource_metrics
            for scope_metrics in resource_metrics.scope_metrics
            for metric in scope_metrics.metrics
            if metric.name == name
            for point in metric.data.data_points
        ]


class Telemetry:
    """Captures spans and metrics in memory; reading them shuts the SDK down to flush."""

    current: "Telemetry | None" = None

    def __init__(self) -> None:
        self.span_exporter = InMemorySpanExporter()
        self.metrics = CollectedMetrics()

    def configure(self, **overrides: Any) -> None:
        settings: dict[str, Any] = {
            "service_name": "test-service",
            "auditing": {"enabled": False},
            "logging": {"enabled": False},
            "tracing": {"exporter": TEST_EXPORTER},
            "metrics": {"exporter": TEST_EXPORTER},
        }
        pyobservability.configure(**{**settings, **overrides})

    def spans(self) -> list[ReadableSpan]:
        pyobservability.shutdown()
        return list(self.span_exporter.get_finished_spans())

    def points(self, name: str) -> list[Any]:
        pyobservability.shutdown()
        return self.metrics.points(name)


def _current_telemetry() -> Telemetry:
    if Telemetry.current is None:
        raise RuntimeError("use the `telemetry` fixture")
    return Telemetry.current


span_exporters.register(TEST_EXPORTER, lambda **_: _current_telemetry().span_exporter)
metric_exporters.register(TEST_EXPORTER, lambda **_: _current_telemetry().metrics)


@pytest.fixture
def telemetry() -> Iterator[Telemetry]:
    Telemetry.current = Telemetry()
    yield Telemetry.current
    Telemetry.current = None


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _run(*command: str) -> None:
    subprocess.run(command, check=True, capture_output=True)  # noqa: S603 - fixed local binaries


@pytest.fixture(scope="session")
def postgres_dsn(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """A throwaway server on a private Unix socket; no TCP listener, no container."""
    if shutil.which("initdb") is None:
        pytest.skip("PostgreSQL server binaries are not installed")
    root = tmp_path_factory.mktemp("postgres")
    data, sockets, port = root / "data", root / "sockets", _free_port()
    sockets.mkdir()
    user = getpass.getuser()
    _run("initdb", "-D", str(data), "-A", "trust", "-U", user, "-E", "UTF8", "--no-locale")
    options = f"-k {sockets} -c listen_addresses='' -p {port} -c fsync=off"
    _run("pg_ctl", "-D", str(data), "-o", options, "-w", "-l", str(root / "log"), "start")
    try:
        yield f"host={sockets} port={port} user={user} dbname=postgres"
    finally:
        _run("pg_ctl", "-D", str(data), "-m", "immediate", "stop")
