import logging
from typing import Any

from opentelemetry.util.types import Attributes

from pyobservability.config import MetricsConfig, SecurityConfig
from pyobservability.exceptions import SecurityPolicyError
from pyobservability.security.filters import SECRET_KEYS, KeyMatcher
from pyobservability.security.redaction import Redactor

PER_REQUEST_KEYS = frozenset(
    {"user_id", "request_id", "correlation_id", "trace_id", "span_id", "session_id"}
)
_DECISION_CACHE_LIMIT = 4096

_logger = logging.getLogger("pyobservability.metrics")


class LabelPolicy:
    """Keeps secrets, PII and unbounded per-request values out of metric labels.

    Label keys and values are low-cardinality by design, so both decisions are cached.
    """

    def __init__(
        self, metrics: MetricsConfig, security: SecurityConfig, redactor: Redactor
    ) -> None:
        self._allowed = metrics.allowed_label_keys
        self._forbidden = KeyMatcher(
            SECRET_KEYS
            | security.extra_secret_keys
            | security.pii_fields
            | PER_REQUEST_KEYS
            | metrics.forbidden_label_keys
        )
        self._rejects = metrics.on_forbidden_label == "reject"
        self._redactor = redactor
        self._permitted_by_keys: dict[frozenset[str], frozenset[str]] = {}
        self._redacted_values: dict[str, str] = {}
        self._reported: set[tuple[str, str]] = set()

    def apply(self, instrument: str, attributes: Attributes) -> Attributes:
        if not attributes:
            return attributes
        keys = frozenset(attributes)
        permitted = self._permitted(keys)
        if permitted != keys:
            self._handle_forbidden(instrument, keys - permitted)
        return {key: self._clean(value) for key, value in attributes.items() if key in permitted}

    def _permitted(self, keys: frozenset[str]) -> frozenset[str]:
        permitted = self._permitted_by_keys.get(keys)
        if permitted is None:
            permitted = frozenset(key for key in keys if self._is_permitted(key))
            if len(self._permitted_by_keys) < _DECISION_CACHE_LIMIT:
                self._permitted_by_keys[keys] = permitted
        return permitted

    def _is_permitted(self, key: str) -> bool:
        if self._allowed is not None and key not in self._allowed:
            return False
        return not self._forbidden.matches(key)

    def _handle_forbidden(self, instrument: str, forbidden: frozenset[str]) -> None:
        if self._rejects:
            raise SecurityPolicyError(
                f"metric {instrument!r} uses forbidden labels {sorted(forbidden)}"
            )
        for key in sorted(forbidden):
            if (instrument, key) in self._reported:
                continue
            self._reported.add((instrument, key))
            _logger.warning(
                "metric %s: dropped forbidden label %r (secret, PII or per-request value)",
                instrument,
                key,
            )

    def _clean(self, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        redacted = self._redacted_values.get(value)
        if redacted is None:
            redacted = self._redactor.redact_text(value)
            if len(self._redacted_values) < _DECISION_CACHE_LIMIT:
                self._redacted_values[value] = redacted
        return redacted


class PermissivePolicy:
    def apply(self, instrument: str, attributes: Attributes) -> Attributes:
        return attributes


AttributePolicy = LabelPolicy | PermissivePolicy
