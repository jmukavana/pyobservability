from typing import Protocol


class Component(Protocol):
    @property
    def name(self) -> str: ...

    def start(self) -> None: ...

    def shutdown(self, timeout_s: float) -> None: ...
