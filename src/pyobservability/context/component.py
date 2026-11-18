from pyobservability.config import ContextConfig
from pyobservability.context.manager import DEFAULT_MAX_VALUE_LENGTH, limits


class ContextComponent:
    name = "context"

    def __init__(self, config: ContextConfig) -> None:
        self._config = config

    def start(self) -> None:
        limits.max_value_length = self._config.max_value_length

    def shutdown(self, timeout_s: float) -> None:
        limits.max_value_length = DEFAULT_MAX_VALUE_LENGTH
