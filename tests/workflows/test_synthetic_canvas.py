"""Verify the shared acceptance transport cannot fall back to live Canvas."""

import httpx
import pytest
from synthetic_canvas import SyntheticCanvas


@pytest.mark.parametrize(
    "method,url",
    [
        ("GET", "https://real.example/api/v1/courses"),
        ("GET", "https://canvas.invalid/api/v1/unexpected"),
        ("POST", "https://canvas.invalid/api/v1/courses/42/assignments"),
    ],
)
def test_unexpected_routes_origins_and_writes_fail_closed(method, url):
    canvas = SyntheticCanvas()
    with httpx.Client(transport=httpx.MockTransport(canvas.respond)) as client:
        with pytest.raises(AssertionError):
            client.request(method, url)


def test_pagination_preserves_filters_and_models_missing_submission_fields():
    canvas = SyntheticCanvas()
    with httpx.Client(transport=httpx.MockTransport(canvas.respond)) as client:
        page = client.get(
            "https://canvas.invalid/api/v1/courses/42/assignments/34/submissions",
            params={"include[]": "user", "per_page": 100},
        )
        assert [item["user_id"] for item in page.json()] == [101, 102]
        next_url = page.links["next"]["url"]
        assert httpx.URL(next_url).params["include[]"] == "user"
        second = client.get(next_url)
        assert [item["user_id"] for item in second.json()] == [103, 104]
        assert "submitted_at" not in second.json()[1]
        assert "next" not in second.links
    assert not canvas.writes
