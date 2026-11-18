import importlib
import sys
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import TracebackType
from typing import Any

import pytest
import requests
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import pyobservability
from pyobservability import ConfigurationError, MissingExtraError, instrument, uninstrument
from pyobservability.exporters import span_exporters

MEMORY_EXPORTER = "memory-instrumentation"


class CurrentExporter:
    exporter = InMemorySpanExporter()


if MEMORY_EXPORTER not in span_exporters.names():
    span_exporters.register(MEMORY_EXPORTER, lambda **_: CurrentExporter.exporter)


class _OkHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, format: str, *args: Any) -> None:
        pass


@pytest.fixture
def server_url() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _OkHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()


@pytest.fixture
def instrumented_requests() -> Iterator[None]:
    instrument("requests")
    yield
    uninstrument("requests")


class TestHttpInstrumentation:
    @pytest.mark.usefixtures("instrumented_requests")
    def test_outgoing_requests_are_traced_and_redacted(self, server_url: str) -> None:
        CurrentExporter.exporter = InMemorySpanExporter()
        pyobservability.configure(
            service_name="svc",
            auditing={"enabled": False},
            logging={"enabled": False},
            tracing={"exporter": MEMORY_EXPORTER},
        )

        requests.get(f"{server_url}/claims?api_key=s3cr3t", timeout=5)
        pyobservability.shutdown()
        [span] = CurrentExporter.exporter.get_finished_spans()

        assert span.attributes is not None
        status_code = span.attributes.get("http.response.status_code")
        legacy_status_code = span.attributes.get("http.status_code")
        assert 200 in (status_code, legacy_status_code)
        assert "s3cr3t" not in repr(span.attributes)

    @pytest.mark.usefixtures("instrumented_requests")
    def test_instrumenting_twice_is_harmless(self) -> None:
        instrument("requests")

    def test_unknown_target_lists_supported_ones(self) -> None:
        with pytest.raises(ConfigurationError, match="supported: httpx, requests"):
            instrument("ftp")

    def test_missing_extra_names_the_install_command(self, monkeypatch: pytest.MonkeyPatch) -> None:
        real_import = importlib.import_module

        def without_instrumentation(name: str, package: str | None = None) -> Any:
            if name.startswith("opentelemetry.instrumentation."):
                raise ImportError(name)
            return real_import(name, package)

        monkeypatch.setattr(importlib, "import_module", without_instrumentation)

        with pytest.raises(MissingExtraError, match=r"pyobservability\[http\]"):
            instrument("httpx")


class HookRecorder:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def excepthook(
        self, exc_type: type[BaseException], exc: BaseException, tb: TracebackType | None
    ) -> None:
        self.calls.append(f"sys:{exc}")

    def threading_hook(self, args: threading.ExceptHookArgs) -> None:
        self.calls.append(f"thread:{args.exc_value}")


@pytest.fixture
def previous_hooks(monkeypatch: pytest.MonkeyPatch) -> HookRecorder:
    recorder = HookRecorder()
    monkeypatch.setattr(sys, "excepthook", recorder.excepthook)
    monkeypatch.setattr(threading, "excepthook", recorder.threading_hook)
    return recorder


def configure_hooks() -> None:
    pyobservability.configure(
        service_name="svc",
        auditing={"enabled": False},
        tracing={"enabled": False},
        logging={"capture_unhandled_exceptions": True, "output": "stderr"},
    )


class TestUnhandledExceptionHooks:
    def test_uncaught_exception_is_logged_then_chained(
        self, previous_hooks: HookRecorder, capsys: pytest.CaptureFixture[str]
    ) -> None:
        configure_hooks()

        error = RuntimeError("worker crashed with token=abc")
        sys.excepthook(RuntimeError, error, None)
        logged = capsys.readouterr().err

        assert '"level": "CRITICAL"' in logged
        assert "unhandled exception" in logged
        assert previous_hooks.calls == ["sys:worker crashed with token=abc"]

    def test_thread_exception_is_logged_then_chained(
        self, previous_hooks: HookRecorder, capsys: pytest.CaptureFixture[str]
    ) -> None:
        configure_hooks()

        def crash() -> None:
            raise ValueError("thread failed")

        thread = threading.Thread(target=crash, name="worker-1")
        thread.start()
        thread.join()

        assert '"thread": "worker-1"' in capsys.readouterr().err
        assert previous_hooks.calls == ["thread:thread failed"]

    def test_keyboard_interrupt_is_not_logged(
        self, previous_hooks: HookRecorder, capsys: pytest.CaptureFixture[str]
    ) -> None:
        configure_hooks()

        sys.excepthook(KeyboardInterrupt, KeyboardInterrupt(), None)

        assert capsys.readouterr().err == ""
        assert len(previous_hooks.calls) == 1

    def test_shutdown_restores_previous_hooks(self, previous_hooks: HookRecorder) -> None:
        configure_hooks()

        pyobservability.shutdown()

        assert sys.excepthook == previous_hooks.excepthook
        assert threading.excepthook == previous_hooks.threading_hook
