from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastmcp import FastMCP

from canvas_mcp.tools.new_quizzes import _unsigned_media_url, register_new_quiz_tools


async def _tool(name="upload_new_quiz_media"):
    server = FastMCP("media-test")
    register_new_quiz_tools(server)
    return next(tool.fn for tool in await server.list_tools() if tool.name == name)


@pytest.fixture
def media_config():
    with patch("canvas_mcp.tools.new_quizzes.get_config", return_value=SimpleNamespace(
        canvas_api_url="https://canvas.example/api/v1", api_timeout=5,
    )):
        yield


@pytest.mark.parametrize("url", [
    "http://bucket.s3.amazonaws.com/image?X-Amz-Signature=secret",
    "https://bucket.s3.amazonaws.com.evil.example/image?X-Amz-Signature=secret",
    "https://127.0.0.1/image?X-Amz-Signature=secret",
    "https://s3.amazonaws.com@evil.example/image?X-Amz-Signature=secret",
    "https://user:password@s3.amazonaws.com/image?X-Amz-Signature=secret",
    "https://bucket.s3.amazonaws.com:8080/image?X-Amz-Signature=secret",
    "https://bucket.s3.amazonaws.com/image",
    "https://bucket.s3.amazonaws.com/image?X-Amz-Signature=secret#fragment",
    "https://bucket.s3.amazonaws.com/image\n?X-Amz-Signature=secret",
    None,
])
def test_rejects_untrusted_media_destinations(media_config, url):
    assert _unsigned_media_url(url) is None


@pytest.mark.parametrize("host", [
    "canvas.example", "bucket.s3.amazonaws.com", "bucket.s3.us-west-2.amazonaws.com",
    "s3.us-east-1.amazonaws.com", "bucket.s3-us-west-2.amazonaws.com",
])
def test_signed_url_stripped(media_config, host):
    assert _unsigned_media_url(f"https://{host}/item_media/1?X-Amz-Signature=secret") == f"https://{host}/item_media/1"


@pytest.mark.asyncio
async def test_hosted_media_upload_refused_before_file_or_network():
    with patch("canvas_mcp.tools.new_quizzes.is_http_request_active", return_value=True), patch(
        "canvas_mcp.tools.new_quizzes.validate_file_for_upload"
    ) as validation, patch("canvas_mcp.tools.new_quizzes.make_canvas_request", new_callable=AsyncMock) as request:
        result = await (await _tool())("42", "12", "/private/token.png")
    assert "stdio" in result["error"]
    validation.assert_not_called()
    request.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 204, 307, 500, "timeout"])
async def test_binary_upload_does_not_leak_token_follow_redirect_or_retry(tmp_path, media_config, status):
    path = tmp_path / "picture.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\nexample")
    seen = []

    def respond(request):
        seen.append(request)
        if status == "timeout":
            raise httpx.ReadTimeout("secret signed URL", request=request)
        return httpx.Response(status, headers={"Location": "https://evil.example/steal"})

    storage = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    signed = "https://bucket.s3.amazonaws.com/item_media/1?X-Amz-Signature=secret"
    with patch("canvas_mcp.tools.new_quizzes.is_http_request_active", return_value=False), patch(
        "canvas_mcp.tools.new_quizzes.get_course_id", new=AsyncMock(return_value="42")
    ), patch("canvas_mcp.tools.new_quizzes.make_canvas_request", new=AsyncMock(return_value={"url": signed})) as request, patch(
        "canvas_mcp.tools.new_quizzes.httpx.AsyncClient", return_value=storage
    ) as client:
        result = await (await _tool())("42", "12", str(path))
    request.assert_awaited_once_with("get", "/courses/42/quizzes/12/items/media_upload_url", api_root="quiz")
    assert client.call_args.kwargs["follow_redirects"] is False
    assert client.call_args.kwargs["trust_env"] is False
    assert len(seen) == 1
    assert seen[0].method == "PUT"
    assert seen[0].content == path.read_bytes()
    assert "authorization" not in seen[0].headers
    assert seen[0].headers["content-type"] == "image/png"
    assert "secret" not in str(result)
    assert "X-Amz" not in str(result)
    if status in {200, 204}:
        assert result["image_url"] == signed.split("?")[0]
        assert result["size_bytes"] == path.stat().st_size
    else:
        assert "error" in result


@pytest.mark.asyncio
async def test_invalid_media_slot_sends_no_file(tmp_path, media_config):
    path = tmp_path / "picture.png"
    path.write_bytes(b"png")
    with patch("canvas_mcp.tools.new_quizzes.is_http_request_active", return_value=False), patch(
        "canvas_mcp.tools.new_quizzes.get_course_id", new=AsyncMock(return_value="42")
    ), patch("canvas_mcp.tools.new_quizzes.make_canvas_request", new=AsyncMock(return_value={"url": "https://evil.example/image"})), patch(
        "canvas_mcp.tools.new_quizzes.httpx.AsyncClient"
    ) as storage:
        result = await (await _tool())("42", "12", str(path))
    assert "No file was sent" in result["error"]
    storage.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,algorithm,scoring", [
    ("numeric", "Numeric", {"value": 42}),
    ("formula", "Numeric", {"value": "x+y"}),
])
async def test_create_sends_required_empty_interaction_data(kind, algorithm, scoring):
    with patch("canvas_mcp.tools.new_quizzes.get_course_id", new=AsyncMock(return_value="42")), patch(
        "canvas_mcp.tools.new_quizzes.make_canvas_request", new=AsyncMock(return_value={"id": 1})
    ) as request:
        await (await _tool("create_new_quiz_question"))("42", "12", "Solve", kind, algorithm, scoring)
    assert request.await_args.kwargs["data"]["item"]["entry"]["interaction_data"] == {}


@pytest.mark.asyncio
async def test_partial_update_does_not_erase_interaction_data():
    with patch("canvas_mcp.tools.new_quizzes.get_course_id", new=AsyncMock(return_value="42")), patch(
        "canvas_mcp.tools.new_quizzes.make_canvas_request", new=AsyncMock(return_value={"id": 1})
    ) as request:
        await (await _tool("update_new_quiz_question"))("42", "12", "1", title="Rename")
    assert request.await_args.kwargs["data"]["item"]["entry"] == {"title": "Rename"}
