from opentelemetry.sdk.resources import Resource
from opentelemetry.semconv._incubating.attributes.deployment_attributes import (
    DEPLOYMENT_ENVIRONMENT_NAME,
)
from opentelemetry.semconv.attributes.service_attributes import SERVICE_NAME, SERVICE_VERSION

from pyobservability.config import ObservabilityConfig


def build_resource(config: ObservabilityConfig) -> Resource:
    return Resource.create(
        {
            **config.resource_attributes,
            SERVICE_NAME: config.service_name,
            SERVICE_VERSION: config.service_version,
            DEPLOYMENT_ENVIRONMENT_NAME: config.environment,
        }
    )
