"""Credential boundaries for Canvas file transfers."""

from types import SimpleNamespace

import httpx
import pytest

from canvas_mcp.core import client, config
from canvas_mcp.core.credentials import (
    RequestCredentials,
    clear_http_request_context,
    set_http_request_active,
    set_request_credentials,
)


@pytest.fixture
def transport_clients(monkeypatch):
    requests = []
    responses = {}

    def handle(request):
        requests.append(request)
        status, headers = responses.get(str(request.url), (200, {}))
        return httpx.Response(status, headers=headers, json={"id": 123})

    original = httpx.AsyncClient
    def factory(*args, **kwargs):
        return original(*args, transport=httpx.MockTransport(handle), **kwargs)

    monkeypatch.setattr(config, "get_config", lambda: SimpleNamespace(
        canvas_api_url="https://canvas.example/api/v1", api_timeout=5,
        canvas_api_token="server-secret", log_api_requests=False,
    ))
    monkeypatch.setattr(client.httpx, "AsyncClient", factory)
    monkeypatch.setattr(client, "_get_http_client", lambda: factory(headers={
        "Authorization": "Bearer server-secret"
    }))
    yield requests, responses
    clear_http_request_context()


@pytest.mark.asyncio
@pytest.mark.parametrize("url,authenticated", [
    ("https://canvas.example/files/1", True),
    ("https://canvas.example:443/files/1", True),
    ("https://canvas.example.evil/files/1", False),
    ("https://canvas.example:444/files/1", False),
    ("https://canvas.example:0/files/1", False),
    ("http://canvas.example/files/1", False),
])
async def test_download_origin_credentials(transport_clients, url, authenticated):
    requests, _ = transport_clients
    set_http_request_active()
    set_request_credentials(RequestCredentials("caller-secret", "https://canvas.example/api/v1"))
    async with client.file_download_client(url) as session:
        await session.get(url, follow_redirects=True)
    assert requests[0].headers.get("authorization") == ("Bearer caller-secret" if authenticated else None)


@pytest.mark.asyncio
async def test_download_missing_http_credentials_fails_before_request(transport_clients):
    requests, _ = transport_clients
    set_http_request_active()
    with pytest.raises(PermissionError):
        async with client.file_download_client("https://storage.example/file"):
            pass
    assert not requests


@pytest.mark.asyncio
async def test_download_redirect_drops_canvas_credentials(transport_clients):
    requests, responses = transport_clients
    responses["https://canvas.example/file"] = (302, {"Location": "https://storage.example/file?signature=secret"})
    async with client.file_download_client("https://canvas.example/file") as session:
        await session.get("https://canvas.example/file", follow_redirects=True)
    assert requests[0].headers["authorization"] == "Bearer server-secret"
    assert "authorization" not in requests[1].headers


@pytest.mark.asyncio
@pytest.mark.parametrize("location", [
    "https://canvas.example.evil/confirm", "https://canvas.example:444/confirm",
    "http://canvas.example/confirm", "/relative-confirm", "//evil.example/confirm",
])
async def test_upload_rejects_off_origin_confirmation(transport_clients, tmp_path, location):
    requests, responses = transport_clients
    responses["https://storage.example/upload"] = (302, {"Location": location})
    source = tmp_path / "file.txt"
    source.write_text("hello")
    result = await client.upload_file_to_storage("https://storage.example/upload", {}, str(source), source.name, "text/plain")
    assert "error" in result
    assert len(requests) == 1
    assert "authorization" not in requests[0].headers


@pytest.mark.asyncio
async def test_upload_confirmation_does_not_follow_redirect(transport_clients, tmp_path):
    requests, responses = transport_clients
    responses["https://storage.example/upload"] = (302, {"Location": "https://canvas.example/confirm"})
    responses["https://canvas.example/confirm"] = (302, {"Location": "https://evil.example/steal"})
    source = tmp_path / "file.txt"
    source.write_text("hello")
    result = await client.upload_file_to_storage("https://storage.example/upload", {}, str(source), source.name, "text/plain")
    assert "error" in result
    assert len(requests) == 2
    assert requests[1].headers["authorization"] == "Bearer server-secret"


@pytest.mark.asyncio
async def test_download_rejects_url_userinfo(transport_clients):
    requests, _ = transport_clients
    with pytest.raises(ValueError):
        async with client.file_download_client("https://canvas.example@evil.example/file"):
            pass
    assert not requests


@pytest.mark.asyncio
async def test_download_upgrade_redirect_drops_credentials(transport_clients, monkeypatch):
    requests, responses = transport_clients
    monkeypatch.setattr(config, "get_config", lambda: SimpleNamespace(
        canvas_api_url="http://canvas.example/api/v1", api_timeout=5,
        canvas_api_token="server-secret", log_api_requests=False,
    ))
    responses["http://canvas.example/file"] = (302, {"Location": "https://canvas.example/file"})
    async with client.file_download_client("http://canvas.example/file") as session:
        await session.get("http://canvas.example/file", follow_redirects=True)
    assert requests[0].headers["authorization"] == "Bearer server-secret"
    assert "authorization" not in requests[1].headers


@pytest.mark.asyncio
async def test_mismatched_caller_origin_fails_closed(transport_clients):
    requests, _ = transport_clients
    set_http_request_active()
    set_request_credentials(RequestCredentials("caller-secret", "https://other.example/api/v1"))
    with pytest.raises(PermissionError):
        async with client.file_download_client("https://canvas.example/file"):
            pass
    assert not requests


@pytest.mark.asyncio
async def test_upload_relative_confirmation_uses_caller_token(transport_clients, tmp_path):
    requests, responses = transport_clients
    responses["https://canvas.example/upload"] = (302, {"Location": "/api/v1/files/123/confirm"})
    source = tmp_path / "file.txt"
    source.write_text("hello")
    set_http_request_active()
    set_request_credentials(RequestCredentials("caller-secret", "https://canvas.example/api/v1"))
    result = await client.upload_file_to_storage(
        "https://canvas.example/upload", {}, str(source), source.name, "text/plain"
    )
    assert result == {"id": 123}
    assert "authorization" not in requests[0].headers
    assert str(requests[1].url) == "https://canvas.example/api/v1/files/123/confirm"
    assert requests[1].headers["authorization"] == "Bearer caller-secret"


@pytest.mark.asyncio
async def test_upload_mismatched_caller_origin_fails_closed(transport_clients, tmp_path):
    requests, responses = transport_clients
    responses["https://storage.example/upload"] = (302, {"Location": "https://canvas.example/confirm"})
    source = tmp_path / "file.txt"
    source.write_text("hello")
    set_http_request_active()
    set_request_credentials(RequestCredentials("caller-secret", "https://other.example/api/v1"))
    result = await client.upload_file_to_storage(
        "https://storage.example/upload", {}, str(source), source.name, "text/plain"
    )
    assert "error" in result
    assert len(requests) == 1
