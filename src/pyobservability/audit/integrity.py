import hashlib
import hmac
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from pyobservability.audit.schemas import AuditEvent
from pyobservability.config import IntegrityConfig
from pyobservability.exceptions import ConfigurationError

GLOBAL_CHAIN = "_global"
GENESIS_HASH = ""
SHA256_PREFIX = "sha256:"
HMAC_PREFIX = "hmac-sha256:"


@dataclass(frozen=True, slots=True)
class ChainLink:
    seq: int
    prev_hash: str
    hash: str
    key_id: str | None


@dataclass(frozen=True, slots=True)
class StoredEvent:
    link: ChainLink
    event: AuditEvent


@dataclass(frozen=True, slots=True)
class VerifyResult:
    ok: bool
    checked: int
    first_bad_seq: int | None = None
    problem: str | None = None


def chain_key(event: AuditEvent) -> str:
    """One chain per tenant, so verifying a tenant never needs another tenant's records."""
    return event.tenant_id or GLOBAL_CHAIN


def canonical_bytes(event: AuditEvent) -> bytes:
    payload = event.model_dump(mode="json")
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


class ChainSigner:
    """Links events into a hash chain; with HMAC keys a database-only attacker cannot re-chain."""

    def __init__(self, keys: Mapping[str, bytes], active_key_id: str | None) -> None:
        if active_key_id is not None and active_key_id not in keys:
            raise ConfigurationError(f"integrity key {active_key_id!r} is not configured")
        self._keys = dict(keys)
        self._active_key_id = active_key_id

    @classmethod
    def from_config(cls, config: IntegrityConfig) -> "ChainSigner":
        keys = {key_id: key.get_secret_value().encode() for key_id, key in config.keys.items()}
        return cls(keys, config.active_key_id)

    def link(self, event: AuditEvent, previous: ChainLink | None) -> ChainLink:
        seq = 1 if previous is None else previous.seq + 1
        prev_hash = GENESIS_HASH if previous is None else previous.hash
        digest = self.digest(event, prev_hash, self._active_key_id)
        return ChainLink(seq, prev_hash, digest, self._active_key_id)

    def digest(self, event: AuditEvent, prev_hash: str, key_id: str | None) -> str:
        message = canonical_bytes(event) + prev_hash.encode()
        if key_id is None:
            return SHA256_PREFIX + hashlib.sha256(message).hexdigest()
        if key_id not in self._keys:
            raise KeyError(key_id)
        return HMAC_PREFIX + hmac.new(self._keys[key_id], message, hashlib.sha256).hexdigest()

    def verify(self, chain: Iterable[StoredEvent]) -> VerifyResult:
        """Detects edited, deleted, reordered or inserted records in one ordered chain."""
        previous: ChainLink | None = None
        checked = 0
        for stored in chain:
            problem = self._problem_with(stored, previous)
            if problem is not None:
                return VerifyResult(False, checked, stored.link.seq, problem)
            previous = stored.link
            checked += 1
        return VerifyResult(True, checked)

    def _problem_with(self, stored: StoredEvent, previous: ChainLink | None) -> str | None:
        link = stored.link
        expected_seq = 1 if previous is None else previous.seq + 1
        if link.seq != expected_seq:
            return f"sequence gap: expected {expected_seq}, found {link.seq}"
        expected_prev = GENESIS_HASH if previous is None else previous.hash
        if link.prev_hash != expected_prev:
            return "previous hash does not match the preceding record"
        try:
            recomputed = self.digest(stored.event, link.prev_hash, link.key_id)
        except KeyError:
            return f"record signed with unknown key {link.key_id!r}"
        if not hmac.compare_digest(recomputed, link.hash):
            return "record content does not match its hash"
        return None
