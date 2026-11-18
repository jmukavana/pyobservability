from collections.abc import Mapping, MutableMapping

from opentelemetry import propagate
from opentelemetry.context import Context


def inject_trace_headers(headers: MutableMapping[str, str]) -> None:
    propagate.inject(headers)


def context_from_headers(headers: Mapping[str, str]) -> Context:
    return propagate.extract(headers)
