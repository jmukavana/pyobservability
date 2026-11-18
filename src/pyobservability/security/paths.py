import re

ID_PLACEHOLDER = "{id}"
REDACTED_PLACEHOLDER = "{redacted}"

_ID_SEGMENT = re.compile(
    r"^(?:\d+|[0-9a-fA-F]{16,}|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})$"
)
_SENSITIVE_SEGMENT = re.compile(r"^(?:[^@/]+@[^@/]+\.[^@/]+|[A-Za-z0-9_\-+=.~]{32,})$")


def templated_path(path: str) -> str:
    """`/claims/123/notes/jane@x.io` becomes `/claims/{id}/notes/{redacted}`."""
    return "/".join(_template(segment) for segment in path.split("/"))


def _template(segment: str) -> str:
    if _ID_SEGMENT.match(segment):
        return ID_PLACEHOLDER
    if _SENSITIVE_SEGMENT.match(segment):
        return REDACTED_PLACEHOLDER
    return segment
