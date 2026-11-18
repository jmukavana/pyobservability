import pytest

from pyobservability.security.paths import templated_path


@pytest.mark.parametrize(
    ("path", "templated"),
    [
        ("/claims/123", "/claims/{id}"),
        ("/claims/550e8400-e29b-41d4-a716-446655440000/notes", "/claims/{id}/notes"),
        ("/blobs/9f86d081884c7d659a2feaa0c55ad015", "/blobs/{id}"),
        ("/users/jane.doe@example.com", "/users/{redacted}"),
        ("/reset/eyJhbGciOiJIUzI1NiJ9abcdefghijklmnop", "/reset/{redacted}"),
        ("/v1/claims", "/v1/claims"),
        ("/", "/"),
    ],
)
def test_identifier_segments_are_templated(path: str, templated: str) -> None:
    assert templated_path(path) == templated
