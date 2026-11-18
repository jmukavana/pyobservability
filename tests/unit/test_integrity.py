from dataclasses import replace

import pytest

from pyobservability.audit import AuditEvent
from pyobservability.audit.integrity import (
    GENESIS_HASH,
    HMAC_PREFIX,
    SHA256_PREFIX,
    ChainSigner,
    StoredEvent,
    canonical_bytes,
    chain_key,
)
from pyobservability.exceptions import ConfigurationError

SOURCE = {"service": "claims", "version": "1", "environment": "test", "host": "h"}


def event(resource_id: str = "CLM-1", tenant_id: str | None = "tenant-1") -> AuditEvent:
    return AuditEvent.model_validate(
        {
            "event_type": "CLAIM_APPROVED",
            "resource_type": "CLAIM",
            "resource_id": resource_id,
            "action": "APPROVE",
            "tenant_id": tenant_id,
            "source": SOURCE,
        }
    )


def build_chain(signer: ChainSigner, length: int) -> list[StoredEvent]:
    chain: list[StoredEvent] = []
    for index in range(length):
        previous = chain[-1].link if chain else None
        stored_event = event(f"CLM-{index}")
        chain.append(StoredEvent(signer.link(stored_event, previous), stored_event))
    return chain


@pytest.fixture
def signer() -> ChainSigner:
    return ChainSigner({"2026-11": b"k" * 32}, "2026-11")


class TestLinking:
    def test_first_link_starts_the_chain(self, signer: ChainSigner) -> None:
        link = signer.link(event(), None)

        assert (link.seq, link.prev_hash, link.key_id) == (1, GENESIS_HASH, "2026-11")
        assert link.hash.startswith(HMAC_PREFIX)

    def test_links_reference_their_predecessor(self, signer: ChainSigner) -> None:
        first, second = build_chain(signer, 2)

        assert second.link.seq == 2
        assert second.link.prev_hash == first.link.hash

    def test_unkeyed_chains_use_plain_sha256(self) -> None:
        assert ChainSigner({}, None).link(event(), None).hash.startswith(SHA256_PREFIX)

    def test_canonical_form_ignores_key_order(self) -> None:
        original = event()
        reordered = AuditEvent.model_validate(dict(reversed(original.model_dump().items())))

        assert canonical_bytes(original) == canonical_bytes(reordered)

    def test_chains_are_per_tenant(self) -> None:
        assert chain_key(event(tenant_id="a")) != chain_key(event(tenant_id="b"))
        assert chain_key(event(tenant_id=None)) == "_global"

    def test_unknown_active_key_is_a_configuration_error(self) -> None:
        with pytest.raises(ConfigurationError, match="not configured"):
            ChainSigner({}, "missing")


class TestVerification:
    def test_intact_chain_verifies(self, signer: ChainSigner) -> None:
        result = signer.verify(build_chain(signer, 5))

        assert result.ok
        assert result.checked == 5

    def test_edited_content_is_detected(self, signer: ChainSigner) -> None:
        chain = build_chain(signer, 5)
        forged = chain[2].event.model_copy(update={"actor_id": "someone-else"})
        chain[2] = StoredEvent(chain[2].link, forged)

        result = signer.verify(chain)

        assert not result.ok
        assert (result.first_bad_seq, result.checked) == (3, 2)
        assert result.problem == "record content does not match its hash"

    def test_deleted_record_is_detected(self, signer: ChainSigner) -> None:
        chain = build_chain(signer, 5)
        del chain[1]

        result = signer.verify(chain)

        assert not result.ok
        assert result.problem is not None
        assert "sequence gap" in result.problem

    def test_reordered_records_are_detected(self, signer: ChainSigner) -> None:
        chain = build_chain(signer, 4)
        chain[1], chain[2] = chain[2], chain[1]

        assert not signer.verify(chain).ok

    def test_recomputed_hash_without_the_key_is_detected(self, signer: ChainSigner) -> None:
        chain = build_chain(signer, 3)
        forged = chain[1].event.model_copy(update={"action": "REJECT"})
        attacker = ChainSigner({"2026-11": b"guessed-key"}, "2026-11")
        chain[1] = StoredEvent(attacker.link(forged, chain[0].link), forged)

        assert signer.verify(chain).first_bad_seq == 2

    def test_tampered_prev_hash_is_detected(self, signer: ChainSigner) -> None:
        chain = build_chain(signer, 3)
        chain[2] = replace(chain[2], link=replace(chain[2].link, prev_hash="sha256:forged"))

        assert signer.verify(chain).problem == "previous hash does not match the preceding record"

    def test_records_signed_with_unknown_keys_are_reported(self, signer: ChainSigner) -> None:
        chain = build_chain(signer, 2)

        result = ChainSigner({}, None).verify(chain)

        assert result.problem == "record signed with unknown key '2026-11'"

    def test_rotated_keys_verify_old_and_new_records(self) -> None:
        old_signer = ChainSigner({"old": b"a" * 32}, "old")
        chain = build_chain(old_signer, 2)
        rotated = ChainSigner({"old": b"a" * 32, "new": b"b" * 32}, "new")
        newest = event("CLM-new")
        chain.append(StoredEvent(rotated.link(newest, chain[-1].link), newest))

        assert rotated.verify(chain).ok
        assert chain[-1].link.key_id == "new"

    def test_empty_chain_is_trivially_valid(self, signer: ChainSigner) -> None:
        assert signer.verify([]).ok
