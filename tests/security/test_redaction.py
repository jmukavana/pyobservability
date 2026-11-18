import inspect
import json
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import BaseModel, SecretStr

from pyobservability.config import SecurityConfig
from pyobservability.security import REDACTION_FAILED, Redactor
from pyobservability.security.redaction import (
    CYCLE,
    MAX_DEPTH,
    MAX_ITEMS,
    MAX_STRING_LENGTH,
    TRUNCATED,
)

MASK = "[REDACTED]"

SECRET_KEYS = [
    "password",
    "Password",
    "db_password",
    "userPassword",
    "passwd",
    "pwd",
    "secret",
    "client_secret",
    "clientSecret",
    "token",
    "access_token",
    "refresh_token",
    "id_token",
    "csrfToken",
    "api_key",
    "apiKey",
    "APIKey",
    "x-api-key",
    "X-Api-Key",
    "apikey",
    "authorization",
    "Authorization",
    "proxy-authorization",
    "cookie",
    "set-cookie",
    "session",
    "session_id",
    "private_key",
    "aws_access_key",
    "credentials",
    "ssn",
    "otp",
    "pin",
    "signature",
]

SAFE_KEYS = [
    "claim_id",
    "status",
    "tenant_id",
    "request_id",
    "author",
    "pinned",
    "max_tokens",
    "keys",
]

SECRET_VALUES = [
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
    "Bearer abc123.def-456_ghi",
    "bearer ZXhhbXBsZQ==",
    "Basic dXNlcjpwYXNzd29yZA==",
    "AKIAIOSFODNN7EXAMPLE",
    "ghp_" + "a" * 36,
    "gho_" + "B" * 40,
    "xoxb-1234567890-abcdefghij",
    "sk_live_" + "c" * 24,
    "rk_test_" + "d" * 24,
    "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----",
    "-----BEGIN PRIVATE KEY-----\nMIIEv\n-----END PRIVATE KEY-----",
    "4111111111111111",
    "4111 1111 1111 1111",
    "5500-0000-0000-0004",
    "378282246310005",
]

NOT_SECRETS = [
    "hello world",
    "claim CLM-2001 approved",
    "4111111111111112",
    "order 1234567890123",
    "eyJ-not-a-token",
    "basic plan",
]

PII_VALUES = {
    "jane.doe@example.com": "j***@example.com",
    "+254712345678": "+***",
    "192.168.10.24": "1***",
}


@pytest.fixture
def redactor() -> Redactor:
    return Redactor(SecurityConfig())


def lenient(**overrides: Any) -> Redactor:
    return Redactor(SecurityConfig(**overrides, allow_weakening=True))


class TestSecretKeys:
    @pytest.mark.parametrize("key", SECRET_KEYS)
    def test_values_under_secret_keys_are_masked(self, redactor: Redactor, key: str) -> None:
        assert redactor.redact({key: "visible-value"}) == {key: MASK}

    @pytest.mark.parametrize("key", SAFE_KEYS)
    def test_ordinary_keys_are_kept(self, redactor: Redactor, key: str) -> None:
        assert redactor.redact({key: "visible"}) == {key: "visible"}

    def test_whole_structure_under_secret_key_is_masked(self, redactor: Redactor) -> None:
        assert redactor.redact({"credentials": {"user": "u", "pass": "p"}}) == {"credentials": MASK}

    def test_extra_secret_keys_are_honoured(self) -> None:
        redactor = Redactor(SecurityConfig(extra_secret_keys=frozenset({"iban"})))

        assert redactor.redact({"customer_iban": "DE89370400440532013000"}) == {
            "customer_iban": MASK
        }

    def test_custom_mask_token(self) -> None:
        redactor = Redactor(SecurityConfig(mask_token="***"))

        assert redactor.redact({"password": "p"}) == {"password": "***"}


class TestSecretValues:
    @pytest.mark.parametrize("secret", SECRET_VALUES)
    def test_secret_values_are_masked_anywhere_in_text(
        self, redactor: Redactor, secret: str
    ) -> None:
        redacted = redactor.redact_text(f"before {secret} after")

        assert secret not in redacted
        assert MASK in redacted

    @pytest.mark.parametrize("text", NOT_SECRETS)
    def test_ordinary_text_is_kept(self, redactor: Redactor, text: str) -> None:
        assert redactor.redact_text(text) == text

    def test_url_credentials_are_masked_but_host_kept(self, redactor: Redactor) -> None:
        redacted = redactor.redact_text("postgres://admin:s3cret@db.internal:5432/claims")

        assert redacted == f"postgres://{MASK}@db.internal:5432/claims"

    def test_extra_patterns_are_honoured(self) -> None:
        redactor = Redactor(SecurityConfig(extra_patterns=(r"INT-\d{6}",)))

        assert redactor.redact_text("ref INT-123456") == f"ref {MASK}"

    def test_secrets_in_nested_lists_are_masked(self, redactor: Redactor) -> None:
        payload = {"events": [{"headers": ["Bearer abc.def"]}]}

        assert redactor.redact(payload) == {"events": [{"headers": [MASK]}]}


class TestPersonalData:
    @pytest.mark.parametrize(("value", "masked"), PII_VALUES.items())
    def test_pii_in_text_is_partially_masked(
        self, redactor: Redactor, value: str, masked: str
    ) -> None:
        assert redactor.redact_text(f"contact {value}") == f"contact {masked}"

    def test_pii_fields_are_masked_by_name(self, redactor: Redactor) -> None:
        assert redactor.redact({"first_name": "Jane", "customer_email": "jd"}) == {
            "first_name": "J***",
            "customer_email": "j***",
        }

    def test_non_text_values_under_pii_names_are_kept(self, redactor: Redactor) -> None:
        assert redactor.redact({"email_verified": True}) == {"email_verified": True}

    def test_hash_strategy_is_stable_and_salted(self) -> None:
        first = Redactor(SecurityConfig(pii_strategy="hash", hash_salt=SecretStr("salt-a")))
        second = Redactor(SecurityConfig(pii_strategy="hash", hash_salt=SecretStr("salt-b")))

        value = {"email": "jane@example.com"}

        assert first.redact(value) == first.redact(value)
        assert first.redact(value) != second.redact(value)
        assert first.redact(value)["email"].startswith("hash:")

    def test_drop_strategy_removes_pii_fields(self) -> None:
        redactor = Redactor(SecurityConfig(pii_strategy="drop"))

        assert redactor.redact({"email": "jane@example.com", "status": "ok"}) == {"status": "ok"}

    def test_drop_strategy_masks_pii_in_text(self) -> None:
        redactor = Redactor(SecurityConfig(pii_strategy="drop"))

        assert redactor.redact_text("mail jane@example.com") == f"mail {MASK}"


class TestDisabledProtections:
    def test_secrets_kept_when_masking_disabled(self) -> None:
        redactor = lenient(mask_secrets=False)

        assert redactor.redact({"password": "p", "note": "Bearer abc"}) == {
            "password": "p",
            "note": "Bearer abc",
        }

    def test_pii_kept_when_redaction_disabled(self) -> None:
        redactor = lenient(redact_pii=False)

        assert redactor.redact({"email": "jane@example.com"}) == {"email": "jane@example.com"}


@dataclass
class Customer:
    name: str
    password: str


class Account(BaseModel):
    owner: str
    api_key: str


class TestStructures:
    def test_dataclasses_are_redacted_as_mappings(self, redactor: Redactor) -> None:
        assert redactor.redact(Customer("svc", "p")) == {"name": "svc", "password": MASK}

    def test_pydantic_models_are_redacted_as_mappings(self, redactor: Redactor) -> None:
        assert redactor.redact(Account(owner="svc", api_key="k")) == {
            "owner": "svc",
            "api_key": MASK,
        }

    def test_tuples_and_sets_become_lists(self, redactor: Redactor) -> None:
        assert redactor.redact(("a", "b")) == ["a", "b"]
        assert redactor.redact(frozenset({"a"})) == ["a"]

    def test_safe_scalars_are_unchanged(self, redactor: Redactor) -> None:
        values = [1, 2.5, True, None, datetime(2026, 11, 11, tzinfo=UTC), uuid4()]

        assert redactor.redact(values) == values

    def test_bytes_are_summarised(self, redactor: Redactor) -> None:
        assert redactor.redact(b"\x00secret") == "<7 bytes>"

    def test_unknown_objects_are_redacted_via_repr(self, redactor: Redactor) -> None:
        class Opaque:
            def __repr__(self) -> str:
                return "Opaque(token=Bearer abc.def)"

        assert redactor.redact(Opaque()) == f"Opaque(token={MASK})"

    def test_cycles_are_cut(self, redactor: Redactor) -> None:
        cyclic: dict[str, Any] = {"name": "loop"}
        cyclic["self"] = cyclic

        assert redactor.redact(cyclic) == {"name": "loop", "self": CYCLE}

    def test_shared_non_cyclic_references_are_kept(self, redactor: Redactor) -> None:
        shared = {"x": 1}

        assert redactor.redact({"a": shared, "b": shared}) == {"a": {"x": 1}, "b": {"x": 1}}

    def test_deep_structures_are_truncated(self, redactor: Redactor) -> None:
        nested: dict[str, Any] = {}
        cursor = nested
        for _ in range(MAX_DEPTH + 5):
            cursor["child"] = {}
            cursor = cursor["child"]

        assert TRUNCATED in json.dumps(redactor.redact(nested))

    def test_long_strings_are_truncated(self, redactor: Redactor) -> None:
        redacted = redactor.redact_text("a" * (MAX_STRING_LENGTH * 3))

        assert redacted == "a" * MAX_STRING_LENGTH + TRUNCATED

    def test_secret_straddling_the_truncation_point_is_masked(self, redactor: Redactor) -> None:
        secret = "AKIAIOSFODNN7EXAMPLE"
        text = "a" * (MAX_STRING_LENGTH - 5) + " " + secret

        assert "IOSFO" not in redactor.redact_text(text)

    def test_large_collections_are_truncated(self, redactor: Redactor) -> None:
        redacted = redactor.redact(list(range(MAX_ITEMS + 10)))

        assert len(redacted) == MAX_ITEMS + 1
        assert redacted[-1] == TRUNCATED

    def test_large_mappings_are_truncated(self, redactor: Redactor) -> None:
        redacted = redactor.redact({f"k{index}": index for index in range(MAX_ITEMS + 10)})

        assert len(redacted) == MAX_ITEMS + 1
        assert redacted[TRUNCATED] is True


class TestFailClosed:
    class Exploding:
        def __repr__(self) -> str:
            raise RuntimeError("repr failed")

    def test_redact_fails_closed(self, redactor: Redactor) -> None:
        assert redactor.redact(self.Exploding()) == REDACTION_FAILED

    def test_redact_fields_fails_closed(self, redactor: Redactor) -> None:
        assert redactor.redact_fields({"value": self.Exploding()}) == {"message": REDACTION_FAILED}


json_values = st.recursive(
    st.none() | st.booleans() | st.integers() | st.floats(allow_nan=False) | st.text(),
    lambda children: (
        st.lists(children, max_size=5) | st.dictionaries(st.text(max_size=12), children, max_size=5)
    ),
    max_leaves=25,
)


@given(json_values)
def test_redaction_is_idempotent(value: Any) -> None:
    redactor = Redactor(SecurityConfig())
    once = redactor.redact(value)

    assert redactor.redact(once) == once


@given(st.text(max_size=40), st.sampled_from(SECRET_VALUES))
def test_secrets_never_survive_surrounding_text(prefix: str, secret: str) -> None:
    redacted = Redactor(SecurityConfig()).redact_text(f"{prefix} {secret} ")

    assert secret not in redacted


@pytest.mark.parametrize(
    "text",
    ["1 4111111111111111", "1234 4111 1111 1111 1111", "qty 7 5500-0000-0000-0004 ok"],
)
def test_card_numbers_next_to_other_numbers_are_masked(text: str) -> None:
    redacted = Redactor(SecurityConfig()).redact_text(text)

    assert "1111 1111" not in redacted
    assert "111111111111" not in redacted
    assert "0000-0000" not in redacted


class TestIdentifiersAreNotMistakenForCards:
    """Time-ordered UUIDs and random ones pass the Luhn check by chance about 1 time in 400."""

    LUHN_VALID_UUID = "01a1eca8-6906-7373-8100-c7bcfe88a272"

    def test_a_uuid_whose_digits_pass_luhn_is_kept(self, redactor: Redactor) -> None:
        assert redactor.redact_text(f"event {self.LUHN_VALID_UUID} failed") == (
            f"event {self.LUHN_VALID_UUID} failed"
        )

    @given(st.uuids())
    def test_no_uuid_is_ever_altered(self, value: UUID) -> None:
        text = f"claim {value} / {str(value).upper()}"

        assert Redactor(SecurityConfig()).redact_text(text) == text

    def test_a_card_next_to_a_uuid_is_still_masked(self, redactor: Redactor) -> None:
        redacted = redactor.redact_text(f"card 4111111111111111 for {self.LUHN_VALID_UUID}")

        assert "4111111111111111" not in redacted
        assert self.LUHN_VALID_UUID in redacted

    @pytest.mark.parametrize("text", ["id 4111-1111-1111-1111", "id 5500 0000 0000 0004"])
    def test_hyphen_and_space_separated_cards_are_still_masked(
        self, redactor: Redactor, text: str
    ) -> None:
        assert "1111-1111" not in redactor.redact_text(text)
        assert "0000 0000" not in redactor.redact_text(text)


ADVERSARIAL_LENGTH = 16_000
SLOWEST_ACCEPTABLE_S = 0.25
"""A quadratic rule needs 0.3-0.8 s on these inputs; linear ones need under 0.04 s."""

ADVERSARIAL_TEXTS = {
    "long alphanumeric run": "A" * ADVERSARIAL_LENGTH,
    "query keywords without '='": "?" + "token" * (ADVERSARIAL_LENGTH // 5),
    "query without keywords": "?" + "a" * ADVERSARIAL_LENGTH,
    "url scheme lookalike": "http://" + "a" * ADVERSARIAL_LENGTH,
    "digits and dashes": "1-" * (ADVERSARIAL_LENGTH // 2),
    "digits and spaces": "1 " * (ADVERSARIAL_LENGTH // 2),
    "dotted digits": "1.1.1." * (ADVERSARIAL_LENGTH // 6),
    "email without at-sign": "a." * (ADVERSARIAL_LENGTH // 2),
    "email with long domain": "a@" + "b." * (ADVERSARIAL_LENGTH // 2),
    "repeated key headers": "-----BEGIN PRIVATE KEY-----" * (ADVERSARIAL_LENGTH // 27),
    "plus then digits": "+" + "1" * ADVERSARIAL_LENGTH,
    "almost uuids": "01234567-89ab-cdef-0123-456789abcde " * (ADVERSARIAL_LENGTH // 36),
    "uuid chains": "01234567-89ab-cdef-0123-456789abcdef-" * (ADVERSARIAL_LENGTH // 37),
}


@pytest.mark.parametrize("name", sorted(ADVERSARIAL_TEXTS))
def test_hostile_text_cannot_stall_the_redactor(redactor: Redactor, name: str) -> None:
    started = time.perf_counter()
    redactor.redact_text(ADVERSARIAL_TEXTS[name])

    assert time.perf_counter() - started < SLOWEST_ACCEPTABLE_S


class TestBoundedRulesStillCatchRealValues:
    def test_long_prefix_does_not_hide_an_email(self, redactor: Redactor) -> None:
        redacted = redactor.redact_text("x" * 200 + "jane.doe@example.com")

        assert "jane.doe@example.com" not in redacted
        assert redacted.endswith("@example.com")

    def test_long_prefix_does_not_hide_url_credentials(self, redactor: Redactor) -> None:
        redacted = redactor.redact_text("a" * 500 + "postgres://admin:s3cret@db/claims")

        assert "s3cret" not in redacted

    def test_long_prefix_does_not_hide_a_secret_query_value(self, redactor: Redactor) -> None:
        redacted = redactor.redact_text("?" + "a" * 300 + "&access_token=s3cr3t&page=2")

        assert "s3cr3t" not in redacted
        assert "page=2" in redacted

    def test_a_card_hidden_in_a_long_run_of_numbers_is_masked(self, redactor: Redactor) -> None:
        redacted = redactor.redact_text("7 " * 400 + "4111 1111 1111 1111" + " 7" * 400)

        assert "4111" not in redacted

    def test_many_cards_in_one_run_are_all_masked_without_recursion(
        self, redactor: Redactor
    ) -> None:
        """Sized to fit the input window and the output cap; the low limit fails recursive code."""
        cards = 700
        spare_frames = 150
        previous_limit = sys.getrecursionlimit()
        sys.setrecursionlimit(len(inspect.stack()) + spare_frames)
        try:
            redacted = redactor.redact_text("4111111111111111 " * cards)
        finally:
            sys.setrecursionlimit(previous_limit)

        assert "4111" not in redacted
        assert redacted.count(MASK) == cards

    def test_a_long_pem_key_is_masked_whole(self, redactor: Redactor) -> None:
        body = "MIIEvQIBADANBgkqhkiG9w0BAQEFAASC" * 100
        redacted = redactor.redact_text(
            f"k=-----BEGIN PRIVATE KEY-----{body}-----END PRIVATE KEY-----"
        )

        assert body[:40] not in redacted
        assert redacted == f"k={MASK}"
