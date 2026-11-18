import hashlib
import hmac
from typing import Protocol

from pyobservability.config import SecurityConfig
from pyobservability.exceptions import ConfigurationError

HASH_PREFIX = "hash:"
HASH_HEX_LENGTH = 16
VISIBLE_PREFIX_LENGTH = 1


class PiiStrategy(Protocol):
    @property
    def removes_fields(self) -> bool: ...

    def conceal(self, value: str) -> str: ...


class PartialMask:
    """`jane@example.com` becomes `j***@example.com`; other values keep only their first char."""

    removes_fields = False

    def conceal(self, value: str) -> str:
        local, at, domain = value.partition("@")
        return f"{self._mask(local)}{at}{domain}" if at else self._mask(value)

    @staticmethod
    def _mask(text: str) -> str:
        return f"{text[:VISIBLE_PREFIX_LENGTH]}***"


class KeyedHash:
    """Replaces values with a salted HMAC so equal values stay correlatable without exposure."""

    removes_fields = False

    def __init__(self, salt: bytes) -> None:
        self._salt = salt

    def conceal(self, value: str) -> str:
        digest = hmac.new(self._salt, value.encode(), hashlib.sha256).hexdigest()
        return f"{HASH_PREFIX}{digest[:HASH_HEX_LENGTH]}"


class Removal:
    removes_fields = True

    def __init__(self, mask_token: str) -> None:
        self._mask_token = mask_token

    def conceal(self, value: str) -> str:
        return self._mask_token


def pii_strategy_for(config: SecurityConfig) -> PiiStrategy:
    match config.pii_strategy:
        case "mask":
            return PartialMask()
        case "hash":
            if config.hash_salt is None:
                raise ConfigurationError("pii_strategy='hash' requires security.hash_salt")
            return KeyedHash(config.hash_salt.get_secret_value().encode())
        case "drop":
            return Removal(config.mask_token)
