"""Local cartridge preview, upload and authentication-boundary tests."""

import json
import zipfile
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastmcp import FastMCP

from canvas_mcp.tools import content_imports as imports


@pytest.fixture
def archive(tmp_path):
    path = tmp_path / "course.imscc"
    with zipfile.ZipFile(path, "w") as package:
        package.writestr("imsmanifest.xml", "<manifest/>")
        package.writestr("page.html", "<p>Content</p>")
    return path


@pytest.fixture
def local(monkeypatch):
    imports._IMPORT_GUARD.reset()
    monkeypatch.setattr(imports, "is_http_request_active", lambda: False)
    monkeypatch.setattr(imports, "get_request_credentials", lambda: None)
    monkeypatch.setattr(imports, "get_config", lambda: SimpleNamespace(canvas_api_url="https://canvas.example/api/v1", api_timeout=30))
    monkeypatch.setattr(imports, "_resolve_course", AsyncMock(return_value=("42", {"id": 42, "name": "Course", "course_code": "ABC"}, None)))
    monkeypatch.setattr(imports, "_target_occupancy", AsyncMock(return_value={"total_items": 0, "unavailable": []}))
    monkeypatch.setattr(imports, "_create_import", AsyncMock(return_value={"id": 8, "pre_attachment": {"upload_url": "https://bucket.example/upload", "upload_params": {"signature": "SECRET"}}}))
    monkeypatch.setattr(imports, "_upload_archive", AsyncMock(return_value=True))


async def tool():
    server = FastMCP("content-import-test")
    imports.register_content_import_tools(server)
    return {item.name: item.fn for item in await server.list_tools()}["import_course_content"]


@pytest.mark.asyncio
async def test_preview_is_bound_and_has_no_upload_credentials(archive, local):
    result = await (await tool())("42", str(archive))
    assert result["preview"] and not result["migration_requested"]
    assert result["archive_size_bytes"] == archive.stat().st_size
    assert len(result["archive_sha256"]) == 64
    assert result["target_course_id"] == "42"
    assert "SECRET" not in json.dumps(result)
    assert "upload_url" not in json.dumps(result)
    imports._create_import.assert_not_awaited()
    imports._upload_archive.assert_not_awaited()


@pytest.mark.asyncio
async def test_confirm_uploads_exact_archive_once_and_exposes_recovery_id(archive, local):
    fn = await tool()
    preview = await fn("42", str(archive))
    result = await fn("42", str(archive), confirmation_token=preview["confirmation_token"])
    assert result["migration_id"] == "8"
    assert result["upload_confirmed"] and not result["import_complete"]
    assert result["next_action"]["tool"] == "get_content_migration_status"
    assert imports._upload_archive.await_args.args[-1] == archive.read_bytes()
    assert "SECRET" not in json.dumps(result)
    replay = await fn("42", str(archive), confirmation_token=preview["confirmation_token"])
    assert "error" in replay
    imports._create_import.assert_awaited_once()


@pytest.mark.parametrize("change", ["archive", "occupancy", "course"])
@pytest.mark.asyncio
async def test_confirmation_rejects_changed_inputs(archive, local, change):
    fn = await tool()
    preview = await fn("42", str(archive))
    if change == "archive":
        with zipfile.ZipFile(archive, "a") as package:
            package.writestr("new.html", "new")
    elif change == "occupancy":
        imports._target_occupancy.return_value = {"total_items": 1, "unavailable": []}
    else:
        imports._resolve_course.return_value = ("99", {"id": 99}, None)
    result = await fn("42", str(archive), confirmation_token=preview["confirmation_token"])
    assert "error" in result
    imports._create_import.assert_not_awaited()


@pytest.mark.parametrize("context", ["http", "credentials"])
@pytest.mark.asyncio
async def test_http_refuses_local_paths_before_read(archive, local, monkeypatch, context):
    monkeypatch.setattr(imports, "is_http_request_active", lambda: context == "http")
    monkeypatch.setattr(imports, "get_request_credentials", lambda: object() if context == "credentials" else None)
    with patch.object(imports, "_archive_snapshot") as snapshot:
        result = await (await tool())("42", str(archive))
    assert "HTTP" in result["error"]
    snapshot.assert_not_called()
    imports._resolve_course.assert_not_awaited()


@pytest.mark.asyncio
async def test_ambiguous_creation_is_not_retried_and_recovers_history(archive, local):
    fn = await tool()
    preview = await fn("42", str(archive))
    imports._create_import.side_effect = httpx.ReadTimeout("https://signed.example/?SECRET")
    result = await fn("42", str(archive), confirmation_token=preview["confirmation_token"])
    assert result["migration_start_unconfirmed"]
    assert result["next_action"]["tool"] == "list_content_migrations"
    assert "SECRET" not in json.dumps(result)
    imports._create_import.assert_awaited_once()
    imports._upload_archive.assert_not_awaited()


@pytest.mark.asyncio
async def test_upload_failure_preserves_migration_id_without_retry(archive, local):
    fn = await tool()
    preview = await fn("42", str(archive))
    imports._upload_archive.side_effect = httpx.ReadTimeout("SECRET")
    result = await fn("42", str(archive), confirmation_token=preview["confirmation_token"])
    assert result["migration_id"] == "8"
    assert not result["upload_confirmed"]
    assert result["import_start_unconfirmed"]
    assert "SECRET" not in json.dumps(result)
    imports._upload_archive.assert_awaited_once()


@pytest.mark.parametrize("entry", ["../escape", "/absolute", "folder\\bad", "C:drive"])
def test_unsafe_archive_entries_rejected(tmp_path, entry):
    path = tmp_path / "bad.imscc"
    with zipfile.ZipFile(path, "w") as package:
        package.writestr("imsmanifest.xml", "<manifest/>")
        package.writestr(entry, "content")
    with pytest.raises(ValueError, match="safe relative"):
        imports._archive_snapshot(str(path))


def test_missing_manifest_invalid_zip_and_size_bound(tmp_path, archive, monkeypatch):
    path = tmp_path / "bad.imscc"
    path.write_text("not ZIP")
    with pytest.raises(ValueError, match="valid Common Cartridge"):
        imports._archive_snapshot(str(path))
    with zipfile.ZipFile(path, "w") as package:
        package.writestr("page.html", "content")
    with pytest.raises(ValueError, match="imsmanifest"):
        imports._archive_snapshot(str(path))
    monkeypatch.setattr(imports, "_MAX_ARCHIVE_BYTES", 10)
    with pytest.raises(ValueError, match="256 MiB"):
        imports._archive_snapshot(str(archive))


def test_symlink_archive_entry_refused(tmp_path):
    path = tmp_path / "bad.imscc"
    with zipfile.ZipFile(path, "w") as package:
        package.writestr("imsmanifest.xml", "<manifest/>")
        link = zipfile.ZipInfo("link")
        link.create_system = 3
        link.external_attr = 0o120777 << 16
        package.writestr(link, "target")
    with pytest.raises(ValueError, match="symlink"):
        imports._archive_snapshot(str(path))


@pytest.mark.parametrize("url", [
    "http://storage.example/upload", "https://user:password@storage.example/upload",
    "https://bucket.s3.amazonaws.com/upload#fragment", "https://localhost/upload",
    "https://127.0.0.1/upload", "https://169.254.169.254/upload", "https://storage.example:444/upload",
])
def test_upload_url_validation(url):
    assert imports._https_url(url) is None


@pytest.mark.parametrize("location", [
    "https://evil.example/api/v1/files/8/create_success?uuid=SECRET",
    "http://canvas.example/api/v1/files/8/create_success",
    "https://canvas.example/api/v1/courses/42/delete",
    "https://canvas.example/api/v1/files/8/create_success/other",
    "https://user@canvas.example/api/v1/files/8/create_success",
    "https://canvas.example/api/v2/files/8/create_success",
])
def test_redirect_scope_refuses_auth_leaks(location):
    assert imports._confirmation_url(location, "https://bucket.s3.amazonaws.com/upload", "https://canvas.example/api/v1") is None


@pytest.mark.asyncio
async def test_storage_is_unauthenticated_and_redirect_is_scoped(monkeypatch):
    real_client = httpx.AsyncClient
    seen = []

    def storage_response(request):
        seen.append(request)
        assert "authorization" not in request.headers
        body = request.read()
        assert body.index(b'name="signature"') < body.index(b'name="file"')
        return httpx.Response(302, headers={"Location": "https://canvas.example/api/v1/files/8/create_success?uuid=SECRET"})

    def canvas_response(request):
        seen.append(request)
        assert request.headers["authorization"] == "Bearer TOKEN"
        assert request.url.host == "canvas.example"
        return httpx.Response(200, json={"id": 8, "url": "SECRET"})

    @asynccontextmanager
    async def authenticated():
        async with real_client(transport=httpx.MockTransport(canvas_response), headers={"Authorization": "Bearer TOKEN"}) as client:
            yield client

    monkeypatch.setattr(imports, "canvas_authenticated_client", authenticated)
    monkeypatch.setattr(imports, "get_config", lambda: SimpleNamespace(api_timeout=30))
    monkeypatch.setattr(imports.httpx, "AsyncClient", lambda **kwargs: real_client(transport=httpx.MockTransport(storage_response), **kwargs))
    confirmed = await imports._upload_archive(
        {"upload_url": "https://bucket.s3.amazonaws.com/upload", "upload_params": {"signature": "SIG"}},
        "https://canvas.example/api/v1", "course.imscc", b"archive",
    )
    assert confirmed
    assert len(seen) == 2


@pytest.mark.asyncio
async def test_creation_form_uses_pre_attachment_and_never_follows_redirects(monkeypatch):
    seen = []

    def respond(request):
        seen.append(request)
        assert request.url == "https://canvas.example/api/v1/courses/42/content_migrations"
        assert request.headers["Authorization"] == "Bearer TOKEN"
        assert b"migration_type=common_cartridge_importer" in request.content
        assert b"pre_attachment%5Bname%5D=course.imscc" in request.content
        assert b"pre_attachment%5Bsize%5D=123" in request.content
        return httpx.Response(302, headers={"Location": "https://evil.example/?SECRET"})

    @asynccontextmanager
    async def authenticated():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond), headers={"Authorization": "Bearer TOKEN"}) as client:
            yield client

    monkeypatch.setattr(imports, "canvas_authenticated_client", authenticated)
    with pytest.raises(httpx.HTTPStatusError):
        await imports._create_import("https://canvas.example/api/v1", "42", "course.imscc", 123)
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_upload_refuses_foreign_completion_without_authentication(monkeypatch):
    real_client = httpx.AsyncClient
    seen = []

    def respond(request):
        seen.append(request)
        assert "Authorization" not in request.headers
        return httpx.Response(303, headers={"Location": "https://evil.example/api/v1/files/8/create_success?uuid=SECRET"})

    monkeypatch.setattr(imports, "get_config", lambda: SimpleNamespace(api_timeout=30))
    monkeypatch.setattr(imports.httpx, "AsyncClient", lambda **kwargs: real_client(transport=httpx.MockTransport(respond), **kwargs))
    with patch.object(imports, "canvas_authenticated_client") as authenticated:
        confirmed = await imports._upload_archive(
            {"upload_url": "https://bucket.s3.amazonaws.com/upload", "upload_params": {"signature": "SIG"}},
            "https://canvas.example/api/v1", "course.imscc", b"archive",
        )
    assert not confirmed
    assert len(seen) == 1
    authenticated.assert_not_called()


@pytest.mark.asyncio
async def test_storage_timeout_has_no_automatic_retry(monkeypatch):
    real_client = httpx.AsyncClient
    seen = []

    def respond(request):
        seen.append(request)
        raise httpx.ReadTimeout("ambiguous upload")

    monkeypatch.setattr(imports, "get_config", lambda: SimpleNamespace(api_timeout=30))
    monkeypatch.setattr(imports.httpx, "AsyncClient", lambda **kwargs: real_client(transport=httpx.MockTransport(respond), **kwargs))
    with pytest.raises(httpx.ReadTimeout):
        await imports._upload_archive(
            {"upload_url": "https://bucket.s3.amazonaws.com/upload", "upload_params": {"signature": "SIG"}},
            "https://canvas.example/api/v1", "course.imscc", b"archive",
        )
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_direct_201_upload_is_confirmed_without_auth_client(monkeypatch):
    real_client = httpx.AsyncClient
    monkeypatch.setattr(imports, "get_config", lambda: SimpleNamespace(api_timeout=30))
    monkeypatch.setattr(imports.httpx, "AsyncClient", lambda **kwargs: real_client(
        transport=httpx.MockTransport(lambda request: httpx.Response(201, json={"id": 8})), **kwargs
    ))
    with patch.object(imports, "canvas_authenticated_client") as authenticated:
        assert await imports._upload_archive(
            {"upload_url": "https://bucket.s3.amazonaws.com/upload", "upload_params": {"signature": "SIG"}},
            "https://canvas.example/api/v1", "course.imscc", b"archive",
        )
    authenticated.assert_not_called()


@pytest.mark.parametrize("url", [
    "https://evil.example/upload",
    "https://s3.amazonaws.com.evil.example/upload",
    "https://s3-amazonaws.com/upload",
    "https://canvas.example.evil.example/upload",
    "https://canvas.example:444/upload",
    "https://bucket.s3.amazonaws.com:444/upload",
    "http://bucket.s3.amazonaws.com/upload",
    "https://localhost/upload",
    "https://127.0.0.1/upload",
])
def test_storage_slot_host_scope(url):
    assert imports._storage_url(url, "https://canvas.example/api/v1") is None


@pytest.mark.parametrize("url", [
    "https://canvas.example/files/upload",
    "https://bucket.s3.amazonaws.com/upload",
    "https://bucket.s3.us-east-1.amazonaws.com/upload",
    "https://bucket.s3-us-east-1.amazonaws.com/upload",
])
def test_storage_slot_allows_canvas_and_s3(url):
    assert imports._storage_url(url, "https://canvas.example/api/v1") is not None
