import asyncio
import threading
from typing import Any

import pytest
from opentelemetry.sdk.trace import TracerProvider

from pyobservability import configure, context
from pyobservability.config import ContextConfig


class TestAttributes:
    def test_set_and_get(self) -> None:
        context.set(tenant_id="t-1")

        assert context.get("tenant_id") == "t-1"

    def test_missing_key_is_none(self) -> None:
        assert context.get("absent") is None

    def test_set_merges_with_existing_attributes(self) -> None:
        context.set(tenant_id="t-1")
        context.set(user_id="u-1")

        assert context.snapshot() == {"tenant_id": "t-1", "user_id": "u-1"}

    def test_clear_removes_everything(self) -> None:
        context.set(tenant_id="t-1")

        context.clear()

        assert context.snapshot() == {}

    def test_snapshot_is_a_copy(self) -> None:
        context.set(tenant_id="t-1")
        copy = context.snapshot()

        copy["tenant_id"] = "changed"

        assert context.get("tenant_id") == "t-1"

    @pytest.mark.parametrize(
        "key", ["", "1starts_with_digit", "has space", "trailing_newline\n", "x" * 65]
    )
    def test_invalid_keys_are_rejected(self, key: str) -> None:
        with pytest.raises(ValueError, match="invalid context key"):
            context.set(**{key: "value"})

    def test_unsupported_value_types_are_rejected(self) -> None:
        unsupported: Any = {"nested": "dict"}

        with pytest.raises(TypeError, match="must be str, int, float or bool"):
            context.set(payload=unsupported)

    def test_long_values_are_truncated_to_the_configured_limit(self) -> None:
        configure(service_name="svc", auditing={"enabled": False}, context={"max_value_length": 5})

        context.set(note="abcdefgh")

        assert context.get("note") == "abcde"


class TestScope:
    def test_scope_restores_previous_attributes(self) -> None:
        context.set(tenant_id="outer")

        with context.scope(tenant_id="inner", user_id="u-1"):
            assert context.snapshot() == {"tenant_id": "inner", "user_id": "u-1"}

        assert context.snapshot() == {"tenant_id": "outer"}

    def test_scope_restores_after_exception(self) -> None:
        with pytest.raises(RuntimeError), context.scope(request_id="r-1"):
            raise RuntimeError

        assert context.snapshot() == {}

    async def test_async_scope_restores_previous_attributes(self) -> None:
        async with context.scope(request_id="r-1"):
            assert context.get("request_id") == "r-1"

        assert context.get("request_id") is None

    async def test_scope_restores_after_cancellation(self) -> None:
        started = asyncio.Event()

        async def handler() -> None:
            async with context.scope(request_id="r-1"):
                started.set()
                await asyncio.sleep(10)

        task = asyncio.create_task(handler())
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert context.snapshot() == {}


class TestIsolation:
    async def test_concurrent_tasks_never_see_each_others_context(self) -> None:
        async def request(tenant: int) -> bool:
            with context.scope(tenant_id=f"tenant-{tenant}"):
                await asyncio.sleep(0)
                return context.get("tenant_id") == f"tenant-{tenant}"

        results = await asyncio.gather(*(request(tenant) for tenant in range(1000)))

        assert all(results)
        assert context.snapshot() == {}

    def test_threads_start_with_their_own_context(self) -> None:
        context.set(tenant_id="main")
        seen: list[str | int | float | bool | None] = []

        thread = threading.Thread(target=lambda: seen.append(context.get("tenant_id")))
        thread.start()
        thread.join()

        assert seen == [None]

    async def test_to_thread_carries_the_callers_context(self) -> None:
        with context.scope(tenant_id="t-1"):
            assert await asyncio.to_thread(context.get, "tenant_id") == "t-1"


class TestRequestBinding:
    def binder(self, **overrides: Any) -> context.RequestContextBinder:
        return context.RequestContextBinder(ContextConfig(**overrides))

    def test_generates_request_id_when_header_absent(self) -> None:
        with self.binder().bind({}):
            request_id = context.get("request_id")

        assert isinstance(request_id, str)
        assert len(request_id) == 32

    def test_trusts_well_formed_incoming_request_id(self) -> None:
        with self.binder().bind({"x-request-id": "abc-12345678"}):
            assert context.get("request_id") == "abc-12345678"

    @pytest.mark.parametrize(
        "unsafe",
        ["short", "has spaces in it", "inject\nnewline-12345", "trailing-newline\n", "x" * 129],
    )
    def test_replaces_malformed_incoming_request_id(self, unsafe: str) -> None:
        with self.binder().bind({"X-Request-ID": unsafe}):
            assert context.get("request_id") != unsafe

    def test_ignores_incoming_request_id_when_untrusted(self) -> None:
        binder = self.binder(trust_incoming_request_id=False)

        with binder.bind({"X-Request-ID": "abc-12345678"}):
            assert context.get("request_id") != "abc-12345678"

    def test_reads_tenant_from_configured_header(self) -> None:
        with self.binder(tenant_header="X-Tenant").bind({"x-tenant": "acme"}):
            assert context.get("tenant_id") == "acme"

    def test_binding_ends_with_the_scope(self) -> None:
        with self.binder().bind({}):
            pass

        assert context.snapshot() == {}


def test_current_ids_include_trace_and_request_ids() -> None:
    tracer = TracerProvider().get_tracer(__name__)
    with tracer.start_as_current_span("work") as span, context.scope(request_id="r-12345678"):
        ids = context.current_ids()

    assert ids == {
        "trace_id": format(span.get_span_context().trace_id, "032x"),
        "span_id": format(span.get_span_context().span_id, "016x"),
        "request_id": "r-12345678",
    }


def test_current_ids_are_empty_outside_spans_and_requests() -> None:
    assert context.current_ids() == {}
