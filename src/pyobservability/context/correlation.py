import re
import uuid
from collections.abc import Mapping

from opentelemetry import trace

from pyobservability.config import ContextConfig
from pyobservability.context.manager import ContextValue, Scope, get

REQUEST_ID = "request_id"
TENANT_ID = "tenant_id"
USER_ID = "user_id"

_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{8,128}$")


def new_request_id() -> str:
    return uuid.uuid4().hex


def current_ids() -> dict[str, str]:
    ids: dict[str, str] = {}
    span_context = trace.get_current_span().get_span_context()
    if span_context.is_valid:
        ids["trace_id"] = format(span_context.trace_id, "032x")
        ids["span_id"] = format(span_context.span_id, "016x")
    if request_id := get(REQUEST_ID):
        ids[REQUEST_ID] = str(request_id)
    return ids


class RequestContextBinder:
    """Turns inbound request headers into a context scope with request and tenant IDs."""

    def __init__(self, config: ContextConfig) -> None:
        self._config = config

    def bind(self, headers: Mapping[str, str]) -> Scope:
        normalized = {name.lower(): value for name, value in headers.items()}
        attributes: dict[str, ContextValue] = {REQUEST_ID: self._request_id_from(normalized)}
        if tenant_id := self._tenant_id_from(normalized):
            attributes[TENANT_ID] = tenant_id
        return Scope(attributes)

    def _request_id_from(self, headers: Mapping[str, str]) -> str:
        incoming = headers.get(self._config.request_id_header.lower(), "")
        if self._config.trust_incoming_request_id and _SAFE_REQUEST_ID.match(incoming):
            return incoming
        return new_request_id()

    def _tenant_id_from(self, headers: Mapping[str, str]) -> str | None:
        if self._config.tenant_header is None:
            return None
        return headers.get(self._config.tenant_header.lower()) or None
