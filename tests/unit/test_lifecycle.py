import time
from dataclasses import dataclass, field

import pytest

from pyobservability.core.lifecycle import LifecycleManager


@dataclass
class RecordingComponent:
    name: str
    events: list[str]
    fail_on_start: bool = False
    fail_on_shutdown: bool = False
    shutdown_delay_s: float = 0.0
    budgets: list[float] = field(default_factory=list)

    def start(self) -> None:
        if self.fail_on_start:
            raise RuntimeError(f"{self.name} cannot start")
        self.events.append(f"start:{self.name}")

    def shutdown(self, timeout_s: float) -> None:
        self.budgets.append(timeout_s)
        time.sleep(self.shutdown_delay_s)
        self.events.append(f"stop:{self.name}")
        if self.fail_on_shutdown:
            raise RuntimeError(f"{self.name} cannot stop")


def components(events: list[str], *names: str) -> list[RecordingComponent]:
    return [RecordingComponent(name, events) for name in names]


def test_starts_in_order_and_stops_in_reverse() -> None:
    events: list[str] = []
    lifecycle = LifecycleManager(components(events, "a", "b", "c"), shutdown_timeout_s=1)

    lifecycle.start()
    lifecycle.shutdown()

    assert events == ["start:a", "start:b", "start:c", "stop:c", "stop:b", "stop:a"]


def test_failed_start_stops_already_started_components() -> None:
    events: list[str] = []
    failing = RecordingComponent("b", events, fail_on_start=True)
    lifecycle = LifecycleManager([RecordingComponent("a", events), failing], shutdown_timeout_s=1)

    with pytest.raises(RuntimeError, match="b cannot start"):
        lifecycle.start()

    assert events == ["start:a", "stop:a"]


def test_shutdown_error_is_reported_and_others_still_stop() -> None:
    events: list[str] = []
    lifecycle = LifecycleManager(
        [RecordingComponent("a", events), RecordingComponent("b", events, fail_on_shutdown=True)],
        shutdown_timeout_s=1,
    )
    lifecycle.start()

    report = lifecycle.shutdown()

    assert events[-2:] == ["stop:b", "stop:a"]
    assert not report.succeeded
    assert report.components[0].error == "RuntimeError: b cannot stop"


def test_budget_is_shared_across_remaining_components() -> None:
    events: list[str] = []
    first, second = components(events, "first", "second")
    lifecycle = LifecycleManager([first, second], shutdown_timeout_s=1.0)
    lifecycle.start()

    lifecycle.shutdown()

    assert second.budgets[0] == pytest.approx(0.5, abs=0.05)
    assert first.budgets[0] == pytest.approx(1.0, abs=0.05)


def test_overrunning_component_is_reported_as_timed_out() -> None:
    events: list[str] = []
    slow = RecordingComponent("slow", events, shutdown_delay_s=0.05)
    lifecycle = LifecycleManager([slow], shutdown_timeout_s=0.01)
    lifecycle.start()

    report = lifecycle.shutdown()

    assert report.components[0].timed_out
    assert not report.succeeded


def test_second_shutdown_stops_nothing() -> None:
    events: list[str] = []
    lifecycle = LifecycleManager(components(events, "a"), shutdown_timeout_s=1)
    lifecycle.start()
    lifecycle.shutdown()

    report = lifecycle.shutdown()

    assert report.components == ()
    assert events.count("stop:a") == 1
