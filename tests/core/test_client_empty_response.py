from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from canvas_mcp.core import client as cm


@pytest.fixture(autouse=True)
def isolated_client(monkeypatch):
    for name in ("_request_semaphore", "_semaphore_loop_ref"):
        monkeypatch.setattr(cm, name, None)
    config = SimpleNamespace(
        canvas_api_url="https://canvas.example/api/v1",
        max_concurrent_requests=2,
        api_timeout=1,
        log_api_requests=False,
        enable_data_anonymization=False,
        anonymization_debug=False,
    )
    monkeypatch.setattr("canvas_mcp.core.config.get_config", lambda: config)
    monkeypatch.setattr(cm, "get_request_credentials", lambda: None)
    monkeypatch.setattr(cm, "is_http_request_active", lambda: False)


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["delete", "post", "put", "patch"])
async def test_no_content_mutation_returns_empty_success_without_retry(method):
    requests = []

    async def transport(request):
        requests.append(request)
        return httpx.Response(204)

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        with (
            patch.object(cm, "_get_http_client", return_value=client),
            patch.object(cm.asyncio, "sleep", new_callable=AsyncMock) as sleep,
            patch("canvas_mcp.core.audit.log_data_access") as audit,
        ):
            result = await cm.make_canvas_request(
                method, "/courses/42/quizzes/12/items/5", api_root="quiz"
            )

    assert result == {}
    assert len(requests) == 1
    assert requests[0].method == method.upper()
    assert str(requests[0].url) == (
        "https://canvas.example/api/quiz/v1/courses/42/quizzes/12/items/5"
    )
    sleep.assert_not_awaited()
    audit.assert_called_once_with(
        method, "/courses/42/quizzes/12/items/5", "success"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "content"),
    [(200, b""), (201, b""), (200, b"not json"), (204, b"not json")],
)
async def test_unexpected_empty_or_malformed_body_remains_failure(
    status_code, content
):
    requests = []

    async def transport(request):
        requests.append(request)
        return httpx.Response(status_code, content=content)

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        with (
            patch.object(cm, "_get_http_client", return_value=client),
            patch.object(cm.asyncio, "sleep", new_callable=AsyncMock) as sleep,
            patch("canvas_mcp.core.audit.log_data_access") as audit,
        ):
            result = await cm.make_canvas_request("delete", "/courses/42/files/5")

    assert "error" in result
    assert len(requests) == 1
    sleep.assert_not_awaited()
    audit.assert_called_once_with(
        "delete", "/courses/42/files/5", "error", "JSONDecodeError"
    )
