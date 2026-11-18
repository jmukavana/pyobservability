import json
import os
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Self, cast
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue, model_validator

EVENT_TYPE_PATTERN = r"^[A-Z][A-Z0-9_]{2,63}$"
MAX_PAYLOAD_BYTES = 64 * 1024
MAX_PAYLOAD_DEPTH = 6
ALLOWED_CLOCK_SKEW = timedelta(minutes=5)

Category = Literal["business", "security", "access", "admin", "system"]
Outcome = Literal["success", "failure", "denied"]
ActorType = Literal["user", "service", "system", "anonymous"]

_UUID_VERSION_7 = 0x7
_RFC_4122_VARIANT = 0x2
_RANDOM_BYTES = 10


def uuid7() -> UUID:
    """Time-ordered IDs keep audit storage append-friendly; stdlib only has uuid7 from 3.14."""
    if hasattr(uuid, "uuid7"):
        return cast(UUID, uuid.uuid7())
    timestamp_ms = time.time_ns() // 1_000_000
    value = (timestamp_ms << 80) | int.from_bytes(os.urandom(_RANDOM_BYTES))
    value = (value & ~(0xF << 76)) | (_UUID_VERSION_7 << 76)
    value = (value & ~(0x3 << 62)) | (_RFC_4122_VARIANT << 62)
    return UUID(int=value)


def utc_now() -> datetime:
    return datetime.now(UTC)


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Change(_Frozen):
    old: JsonValue = None
    new: JsonValue = None


class Source(_Frozen):
    service: str
    version: str
    environment: str
    host: str


class AuditEvent(_Frozen):
    schema_version: Literal["1"] = "1"
    event_id: UUID = Field(default_factory=uuid7)
    event_type: str = Field(pattern=EVENT_TYPE_PATTERN)
    category: Category = "business"
    outcome: Outcome = "success"
    occurred_at: AwareDatetime = Field(default_factory=utc_now)
    recorded_at: AwareDatetime = Field(default_factory=utc_now)
    actor_id: str | None = Field(default=None, max_length=256)
    actor_type: ActorType = "user"
    tenant_id: str | None = Field(default=None, max_length=256)
    resource_type: str = Field(min_length=1, max_length=128)
    resource_id: str = Field(min_length=1, max_length=256)
    action: str = Field(min_length=1, max_length=64)
    changes: dict[str, Change] = {}
    metadata: dict[str, JsonValue] = {}
    trace_id: str | None = None
    span_id: str | None = None
    request_id: str | None = None
    source: Source

    @model_validator(mode="after")
    def _occurred_at_is_not_in_the_future(self) -> Self:
        if self.occurred_at > self.recorded_at + ALLOWED_CLOCK_SKEW:
            raise ValueError("occurred_at is too far in the future")
        return self

    @model_validator(mode="after")
    def _payload_is_bounded(self) -> Self:
        payload = {"changes": self.model_dump(mode="json")["changes"], "metadata": self.metadata}
        if len(json.dumps(payload)) > MAX_PAYLOAD_BYTES:
            raise ValueError(f"changes and metadata exceed {MAX_PAYLOAD_BYTES} bytes")
        if _depth(payload) > MAX_PAYLOAD_DEPTH + 1:
            raise ValueError(f"changes and metadata nest deeper than {MAX_PAYLOAD_DEPTH} levels")
        return self


def _depth(value: Any) -> int:
    if isinstance(value, dict):
        return 1 + max((_depth(child) for child in value.values()), default=0)
    if isinstance(value, list):
        return 1 + max((_depth(child) for child in value), default=0)
    return 0
