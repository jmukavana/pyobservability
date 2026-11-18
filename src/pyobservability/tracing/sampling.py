from opentelemetry.sdk.trace.sampling import (
    ALWAYS_OFF,
    ALWAYS_ON,
    ParentBased,
    Sampler,
    TraceIdRatioBased,
)

from pyobservability.config import TracingConfig


def sampler_for(config: TracingConfig) -> Sampler:
    match config.sampler:
        case "always_on":
            return ALWAYS_ON
        case "always_off":
            return ALWAYS_OFF
        case "parent_based_ratio":
            return ParentBased(TraceIdRatioBased(config.sample_rate))
