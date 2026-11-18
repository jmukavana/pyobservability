import json
import logging
import time
from pathlib import Path
from typing import Any

import pytest
from opentelemetry.sdk.trace import TracerProvider

import pyobservability
from pyobservability import context, get_logger
from pyobservability.logging.handlers import is_ours

EXPECTED_SERVICE_FIELDS = {
    "service": "claims-service",
    "environment": "development",
    "version": "1.2.3",
}


def configure_logging(**logging_settings: Any) -> None:
    pyobservability.configure(
        service_name="claims-service",
        service_version="1.2.3",
        auditing={"enabled": False},
        logging=logging_settings,
    )


def emitted(captured: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in captured.splitlines() if line.strip()]


def only_record(capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    records = emitted(capsys.readouterr().out)
    assert len(records) == 1, records
    return records[0]


class TestSchema:
    def test_record_has_the_stable_json_schema(self, capsys: pytest.CaptureFixture[str]) -> None:
        configure_logging()

        get_logger("claims").info("Claim approved", claim_id="CLM-001")
        record = only_record(capsys)

        assert list(record)[:4] == ["timestamp", "level", "message", "logger"]
        assert record["level"] == "INFO"
        assert record["message"] == "Claim approved"
        assert record["logger"] == "claims"
        assert record["claim_id"] == "CLM-001"
        assert EXPECTED_SERVICE_FIELDS.items() <= record.items()
        assert {"host", "pid"} <= record.keys()
        assert record["timestamp"].endswith("Z")
        assert "event" not in record

    def test_context_attributes_are_included(self, capsys: pytest.CaptureFixture[str]) -> None:
        configure_logging()

        with context.scope(request_id="req-12345678", tenant_id="tenant-001"):
            get_logger().info("scoped")

        assert {"request_id": "req-12345678", "tenant_id": "tenant-001"}.items() <= only_record(
            capsys
        ).items()

    def test_explicit_fields_override_context(self, capsys: pytest.CaptureFixture[str]) -> None:
        configure_logging()

        with context.scope(tenant_id="from-context"):
            get_logger().info("override", tenant_id="explicit")

        assert only_record(capsys)["tenant_id"] == "explicit"

    def test_trace_ids_are_included_inside_a_span(self, capsys: pytest.CaptureFixture[str]) -> None:
        configure_logging()
        tracer = TracerProvider().get_tracer(__name__)

        with tracer.start_as_current_span("work") as span:
            get_logger().info("traced")
        record = only_record(capsys)

        assert record["trace_id"] == format(span.get_span_context().trace_id, "032x")
        assert record["span_id"] == format(span.get_span_context().span_id, "016x")

    def test_exceptions_are_structured(self, capsys: pytest.CaptureFixture[str]) -> None:
        configure_logging()

        try:
            raise ValueError("bad claim")
        except ValueError:
            get_logger().exception("failed")
        exception = only_record(capsys)["exception"]

        assert exception["type"] == "ValueError"
        assert exception["message"] == "bad claim"
        assert "Traceback" in exception["stacktrace"]


class TestRedaction:
    def test_secret_fields_are_masked(self, capsys: pytest.CaptureFixture[str]) -> None:
        configure_logging()

        get_logger().info("login", password="hunter2", api_key="k-123")
        record = only_record(capsys)

        assert record["password"] == record["api_key"] == "[REDACTED]"

    def test_secrets_in_message_are_masked(self, capsys: pytest.CaptureFixture[str]) -> None:
        configure_logging()

        get_logger().info("calling with Bearer abc.def.ghi")

        assert only_record(capsys)["message"] == "calling with [REDACTED]"

    def test_secrets_in_exception_text_are_masked(self, capsys: pytest.CaptureFixture[str]) -> None:
        configure_logging()

        try:
            raise ConnectionError("postgres://admin:s3cret@db/claims unreachable")
        except ConnectionError:
            get_logger().exception("db down")
        captured = capsys.readouterr().out

        assert "s3cret" not in captured

    def test_other_handlers_never_see_raw_secrets(self) -> None:
        received: list[object] = []

        class SpyHandler(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                received.append(record.msg)

        spy = SpyHandler()
        logging.getLogger().addHandler(spy)
        try:
            configure_logging()
            get_logger().info("login", password="hunter2")
        finally:
            logging.getLogger().removeHandler(spy)

        assert received
        assert "hunter2" not in str(received)

    def test_secrets_in_context_are_masked(self, capsys: pytest.CaptureFixture[str]) -> None:
        configure_logging()

        with context.scope(session_token="abc"):
            get_logger().info("scoped")

        assert only_record(capsys)["session_token"] == "[REDACTED]"


class TestStdlibBridge:
    def test_stdlib_records_share_the_pipeline(self, capsys: pytest.CaptureFixture[str]) -> None:
        configure_logging()

        logging.getLogger("thirdparty").warning("retrying %s with token=%s", "upload", "Bearer x.y")
        record = only_record(capsys)

        assert record["logger"] == "thirdparty"
        assert record["level"] == "WARNING"
        assert record["message"] == "retrying upload with token=[REDACTED]"
        assert record["service"] == "claims-service"

    def test_stdlib_exceptions_are_structured(self, capsys: pytest.CaptureFixture[str]) -> None:
        configure_logging()

        try:
            raise KeyError("missing")
        except KeyError:
            logging.getLogger("thirdparty").exception("lookup failed")

        assert only_record(capsys)["exception"]["type"] == "KeyError"

    def test_stdlib_records_can_be_excluded(self, capsys: pytest.CaptureFixture[str]) -> None:
        configure_logging(capture_stdlib=False)

        logging.getLogger("thirdparty").warning("ignored")
        get_logger("ours").info("kept")

        assert [record["message"] for record in emitted(capsys.readouterr().out)] == ["kept"]


class TestLevelsAndOutputs:
    def test_records_below_the_level_are_dropped(self, capsys: pytest.CaptureFixture[str]) -> None:
        configure_logging(level="WARNING")

        get_logger().info("hidden")
        get_logger().warning("shown")

        assert [record["message"] for record in emitted(capsys.readouterr().out)] == ["shown"]

    def test_stderr_output(self, capsys: pytest.CaptureFixture[str]) -> None:
        configure_logging(output="stderr")

        get_logger().info("to stderr")
        captured = capsys.readouterr()

        assert captured.out == ""
        assert emitted(captured.err)[0]["message"] == "to stderr"

    def test_file_output(self, tmp_path: Path) -> None:
        log_file = tmp_path / "app.log"
        configure_logging(output="file", file_path=log_file)

        get_logger().info("to file")
        pyobservability.shutdown()

        assert emitted(log_file.read_text())[0]["message"] == "to file"

    def test_console_format_is_human_readable(self, capsys: pytest.CaptureFixture[str]) -> None:
        configure_logging(format="console")

        get_logger().info("pretty")
        line = capsys.readouterr().out

        assert "pretty" in line
        assert not line.startswith("{")

    def test_caller_details_are_optional(self, capsys: pytest.CaptureFixture[str]) -> None:
        configure_logging(include_caller=True)

        get_logger().info("where")

        assert only_record(capsys)["func_name"] == "test_caller_details_are_optional"


class TestHandlerLifecycle:
    def our_handlers(self) -> list[logging.Handler]:
        return [handler for handler in logging.getLogger().handlers if is_ours(handler)]

    def test_reconfiguring_never_duplicates_handlers(self) -> None:
        for version in range(5):
            pyobservability.configure(
                service_name="svc",
                service_version=f"{version}",
                auditing={"enabled": False},
                reconfigure=True,
            )

        assert len(self.our_handlers()) == 1

    def test_shutdown_removes_handler_and_restores_root_level(self) -> None:
        root = logging.getLogger()
        previous_level = root.level
        configure_logging(level="DEBUG")

        pyobservability.shutdown()

        assert self.our_handlers() == []
        assert root.level == previous_level

    def test_foreign_handlers_are_left_alone(self) -> None:
        foreign = logging.NullHandler()
        logging.getLogger().addHandler(foreign)
        try:
            configure_logging()
            pyobservability.shutdown()

            assert foreign in logging.getLogger().handlers
        finally:
            logging.getLogger().removeHandler(foreign)

    def test_disabled_logging_installs_nothing(self) -> None:
        configure_logging(enabled=False)

        assert self.our_handlers() == []


class TestFallbackPipeline:
    def test_warnings_before_configure_go_to_stderr_redacted(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        get_logger().warning("early", password="hunter2")
        record = emitted(capsys.readouterr().err)[0]

        assert record["message"] == "early"
        assert record["password"] == "[REDACTED]"

    def test_info_before_configure_is_suppressed(self, capsys: pytest.CaptureFixture[str]) -> None:
        get_logger().info("too early")

        assert capsys.readouterr().err == ""

    def test_logger_obtained_early_follows_later_configuration(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        early_logger = get_logger("early")
        configure_logging()

        early_logger.info("after configure")

        assert only_record(capsys)["service"] == "claims-service"


@pytest.mark.slow
def test_log_call_overhead_stays_within_budget(tmp_path: Path) -> None:
    configure_logging(output="file", file_path=tmp_path / "bench.log")
    logger = get_logger("bench")
    fields = {f"field_{index}": f"value {index}" for index in range(10)}
    iterations = 2000

    started = time.perf_counter()
    for _ in range(iterations):
        logger.info("benchmark", **fields)
    per_call_us = (time.perf_counter() - started) / iterations * 1_000_000

    assert per_call_us < 500, f"{per_call_us:.0f} µs per log call"
