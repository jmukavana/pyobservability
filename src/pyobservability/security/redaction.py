import dataclasses
from collections.abc import Collection, Mapping
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from enum import Enum
from itertools import islice
from typing import Any
from uuid import UUID

from pydantic import BaseModel

from pyobservability.config import SecurityConfig
from pyobservability.security.filters import (
    SECRET_KEYS,
    KeyMatcher,
    TextRule,
    pii_rules,
    secret_rules,
)
from pyobservability.security.masking import PiiStrategy, pii_strategy_for

REDACTION_FAILED = "[REDACTION_FAILED]"
TRUNCATED = "[TRUNCATED]"
CYCLE = "[CYCLE]"
MAX_DEPTH = 10
MAX_STRING_LENGTH = 8192
MAX_ITEMS = 1000

_SAFE_SCALARS = (bool, int, float, type(None), datetime, date, time, timedelta, UUID, Decimal, Enum)


class Redactor:
    """Masks secrets and PII in arbitrary structures. Never raises: it fails closed."""

    def __init__(self, config: SecurityConfig) -> None:
        self._mask_token = config.mask_token
        self._secret_keys = KeyMatcher(SECRET_KEYS | config.extra_secret_keys)
        self._masks_secrets = config.mask_secrets
        self._pii: PiiStrategy | None = pii_strategy_for(config) if config.redact_pii else None
        self._pii_keys = KeyMatcher(config.pii_fields)
        self._text_rules = self._build_text_rules(config)

    def redact(self, value: Any) -> Any:
        try:
            return self._redact(value, depth=0, ancestors=frozenset())
        except Exception:
            return REDACTION_FAILED

    def redact_fields(self, fields: Mapping[str, Any]) -> dict[str, Any]:
        try:
            return self._redact_mapping(fields, depth=0, ancestors=frozenset())
        except Exception:
            return {"message": REDACTION_FAILED}

    def redact_text(self, text: str) -> str:
        window = text[: MAX_STRING_LENGTH * 2]
        for rule in self._text_rules:
            window = rule.pattern.sub(rule.replace, window)
        if len(window) > MAX_STRING_LENGTH:
            return window[:MAX_STRING_LENGTH] + TRUNCATED
        return window

    def _build_text_rules(self, config: SecurityConfig) -> tuple[TextRule, ...]:
        rules: tuple[TextRule, ...] = ()
        if config.mask_secrets:
            rules += secret_rules(config.mask_token, config.extra_patterns)
        if self._pii is not None:
            rules += pii_rules(self._pii.conceal)
        return rules

    def _redact(self, value: Any, depth: int, ancestors: frozenset[int]) -> Any:
        if isinstance(value, str):
            return self.redact_text(value)
        if isinstance(value, _SAFE_SCALARS):
            return value
        if depth >= MAX_DEPTH:
            return TRUNCATED
        if id(value) in ancestors:
            return CYCLE
        return self._redact_container(value, depth, ancestors | {id(value)})

    def _redact_container(self, value: Any, depth: int, ancestors: frozenset[int]) -> Any:
        if isinstance(value, Mapping):
            return self._redact_mapping(value, depth, ancestors)
        if isinstance(value, list | tuple | set | frozenset):
            return self._redact_sequence(value, depth, ancestors)
        if isinstance(value, BaseModel):
            return self._redact_mapping(value.model_dump(), depth, ancestors)
        if dataclasses.is_dataclass(value) and not isinstance(value, type):
            fields = {field.name: getattr(value, field.name) for field in dataclasses.fields(value)}
            return self._redact_mapping(fields, depth, ancestors)
        if isinstance(value, bytes | bytearray | memoryview):
            return f"<{len(value)} bytes>"
        return self.redact_text(repr(value))

    def _redact_sequence(
        self, items: Collection[Any], depth: int, ancestors: frozenset[int]
    ) -> list[Any]:
        redacted = [self._redact(item, depth + 1, ancestors) for item in islice(items, MAX_ITEMS)]
        return [*redacted, TRUNCATED] if len(items) > MAX_ITEMS else redacted

    def _redact_mapping(
        self, mapping: Mapping[Any, Any], depth: int, ancestors: frozenset[int]
    ) -> dict[str, Any]:
        redacted: dict[str, Any] = {}
        for index, (key, value) in enumerate(mapping.items()):
            if index == MAX_ITEMS:
                redacted[TRUNCATED] = True
                break
            name = str(key)
            if self._is_secret_key(name):
                redacted[name] = self._mask_token
            elif self._is_pii_field(name, value):
                self._conceal_pii_field(redacted, name, value)
            else:
                redacted[name] = self._redact(value, depth + 1, ancestors)
        return redacted

    def _is_secret_key(self, name: str) -> bool:
        return self._masks_secrets and self._secret_keys.matches(name)

    def _is_pii_field(self, name: str, value: Any) -> bool:
        is_text_or_number = isinstance(value, str | int) and not isinstance(value, bool)
        return self._pii is not None and is_text_or_number and self._pii_keys.matches(name)

    def _conceal_pii_field(self, redacted: dict[str, Any], name: str, value: str | int) -> None:
        if self._pii is None or self._pii.removes_fields:
            return
        redacted[name] = self._pii.conceal(str(value))
