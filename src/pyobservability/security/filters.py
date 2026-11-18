import re
from bisect import bisect_left, bisect_right
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from functools import lru_cache
from itertools import accumulate

SECRET_KEYS = frozenset(
    {
        "password",
        "passwd",
        "pwd",
        "secret",
        "token",
        "authorization",
        "auth",
        "cookie",
        "credential",
        "credentials",
        "session",
        "ssn",
        "otp",
        "pin",
        "apikey",
        "api_key",
        "access_key",
        "private_key",
        "client_secret",
        "signature",
    }
)

_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_SEPARATORS = re.compile(r"[^a-z0-9]+")


@lru_cache(maxsize=4096)
def normalize_key(key: str) -> str:
    """`X-Api-Key`, `apiKey` and `api_key` all normalize to `x_api_key` / `api_key`."""
    snake = _CAMEL_BOUNDARY.sub("_", key).lower()
    return _SEPARATORS.sub("_", snake).strip("_")


class KeyMatcher:
    """Matches a key when one of the words appears in it as whole `_`-separated tokens."""

    def __init__(self, words: Iterable[str]) -> None:
        self._bounded_words = tuple(f"_{normalize_key(word)}_" for word in words)
        self.matches = lru_cache(maxsize=4096)(self._matches)

    def _matches(self, key: str) -> bool:
        bounded_key = f"_{normalize_key(key)}_"
        return any(word in bounded_key for word in self._bounded_words)


Replacement = Callable[[re.Match[str]], str]


@dataclass(frozen=True, slots=True)
class TextRule:
    pattern: re.Pattern[str]
    replace: Replacement


CARD_NUMBER_LENGTHS = range(13, 20)
MAX_NAME_LENGTH = 64
MAX_SCHEME_LENGTH = 32
MAX_PEM_BODY = 16384
_MIN_CARD_DIGITS = CARD_NUMBER_LENGTHS.start

_KEY_LABEL = r"[A-Z ]{0,24}PRIVATE KEY"
PEM_PRIVATE_KEY = (
    rf"-----BEGIN {_KEY_LABEL}-----[\s\S]{{0,{MAX_PEM_BODY}}}?-----END {_KEY_LABEL}-----"
)
URL_CREDENTIALS = (
    rf"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.-]{{0,{MAX_SCHEME_LENGTH}}}://)[^/\s:@]+:[^/\s@]+@"
)
_QUERY_NAME = rf"[A-Za-z0-9_.-]{{0,{MAX_NAME_LENGTH}}}"
SECRET_QUERY_VALUE = (
    rf"(?i)(?P<name>[?&;]{_QUERY_NAME}"
    rf"(?:token|key|secret|password|passwd|signature|auth|session){_QUERY_NAME}=)[^&#\s]+"
)
_UUID = r"(?<![0-9A-Za-z])[0-9A-Fa-f]{8}(?:-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}(?![0-9A-Za-z])"
_UUID_TOKEN = re.compile(_UUID)
_DIGIT_GROUP = re.compile(r"\d+")


class _LuhnWindows:
    """Luhn checks for any window of one digit string in O(1), from prefix sums.

    A digit's weight depends only on its distance from the window's last digit, so the sums are
    kept per index parity: plain digits, and digits after doubling.
    """

    def __init__(self, digits: str) -> None:
        self._plain = [self._prefix(digits, doubled=False, parity=parity) for parity in (0, 1)]
        self._doubled = [self._prefix(digits, doubled=True, parity=parity) for parity in (0, 1)]

    @staticmethod
    def _prefix(digits: str, *, doubled: bool, parity: int) -> list[int]:
        sums = [0]
        for index, character in enumerate(digits):
            value = int(character)
            counted = sum(divmod(value * 2, 10)) if doubled else value
            sums.append(sums[-1] + (counted if index % 2 == parity else 0))
        return sums

    def is_valid(self, start: int, end: int) -> bool:
        last_parity = (end - 1) % 2
        other = 1 - last_parity
        total = (
            self._plain[last_parity][end]
            - self._plain[last_parity][start]
            + self._doubled[other][end]
            - self._doubled[other][start]
        )
        return total % 10 == 0


def _mask_card_numbers(text: str, mask_token: str) -> str:
    """Neighbouring numbers can join a card's digit run, so test every run of whole groups."""
    groups = list(_DIGIT_GROUP.finditer(text))
    digits = "".join(group.group() for group in groups)
    ends = list(accumulate(len(group.group()) for group in groups))
    starts = [0, *ends[:-1]]
    luhn = _LuhnWindows(digits)
    pieces: list[str] = []
    cursor = first = 0
    while first < len(groups):
        last = _card_ends_at(first, starts, ends, luhn)
        if last is None:
            first += 1
            continue
        pieces += [text[cursor : groups[first].start()], mask_token]
        cursor, first = groups[last].end(), last + 1
    return "".join([*pieces, text[cursor:]])


def _card_ends_at(first: int, starts: list[int], ends: list[int], luhn: _LuhnWindows) -> int | None:
    """Last digit group of the shortest valid card starting at group `first`, if any."""
    begin = starts[first]
    shortest = bisect_left(ends, begin + CARD_NUMBER_LENGTHS.start)
    longest = bisect_right(ends, begin + CARD_NUMBER_LENGTHS.stop - 1)
    return next(
        (index for index in range(shortest, longest) if luhn.is_valid(begin, ends[index])), None
    )


def secret_rules(mask_token: str, extra_patterns: Iterable[str]) -> tuple[TextRule, ...]:
    def masked(_: re.Match[str]) -> str:
        return mask_token

    def masked_card(match: re.Match[str]) -> str:
        text = match.group()
        return text if _UUID_TOKEN.fullmatch(text) else _mask_card_numbers(text, mask_token)

    def masked_url_credentials(match: re.Match[str]) -> str:
        return f"{match.group('scheme')}{mask_token}@"

    def masked_query_value(match: re.Match[str]) -> str:
        return f"{match.group('name')}{mask_token}"

    patterns = [
        (r"eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]+", masked),
        (r"(?i)\bbearer\s+[A-Za-z0-9\-._~+/]+=*", masked),
        (r"(?i)\bbasic\s+[A-Za-z0-9+/]{8,}=*", masked),
        (r"\bAKIA[0-9A-Z]{16}\b", masked),
        (r"\bgh[pousr]_[A-Za-z0-9]{36,}\b", masked),
        (r"\bxox[abprs]-[A-Za-z0-9-]{10,}", masked),
        (r"\b[sr]k_(?:live|test)_[A-Za-z0-9]{16,}\b", masked),
        (PEM_PRIVATE_KEY, masked),
        (URL_CREDENTIALS, masked_url_credentials),
        (SECRET_QUERY_VALUE, masked_query_value),
        (rf"{_UUID}|(?<!\d)(?=(?:[ -]?\d){{{_MIN_CARD_DIGITS}}})\d(?:[ -]?\d)*", masked_card),
        *((pattern, masked) for pattern in extra_patterns),
    ]
    return tuple(TextRule(re.compile(pattern), replace) for pattern, replace in patterns)


EMAIL_PATTERN = r"[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63}){1,8}"
PHONE_PATTERN = r"(?<![\w+])\+\d{7,15}\b"
IPV4_PATTERN = r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b"


def pii_rules(conceal: Callable[[str], str]) -> tuple[TextRule, ...]:
    def concealed(match: re.Match[str]) -> str:
        return conceal(match.group())

    return tuple(
        TextRule(re.compile(pattern), concealed)
        for pattern in (EMAIL_PATTERN, PHONE_PATTERN, IPV4_PATTERN)
    )
