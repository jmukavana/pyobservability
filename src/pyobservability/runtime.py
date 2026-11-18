import threading
from dataclasses import dataclass, field

from pyobservability.config import ObservabilityConfig, ResolvedProviderMode
from pyobservability.core.lifecycle import LifecycleManager
from pyobservability.security import Redactor


@dataclass(frozen=True, slots=True)
class Runtime:
    config: ObservabilityConfig
    provider_mode: ResolvedProviderMode
    redactor: Redactor = field(repr=False, compare=False)
    lifecycle: LifecycleManager = field(repr=False, compare=False)


@dataclass(slots=True)
class _ActiveRuntime:
    lock: threading.RLock = field(default_factory=threading.RLock)
    current: Runtime | None = None


active_runtime = _ActiveRuntime()
