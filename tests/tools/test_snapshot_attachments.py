"""Synthetic scoped attachment streaming, preview, privacy and byte limits."""

import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastmcp import FastMCP

from canvas_mcp.core.artifact_store import ArtifactStore, json_bytes
from canvas_mcp.core.record_snapshots import (
    checkpoint,
    collect_pages,
    new_manifest,
    normalized_scope,
)
from canvas_mcp.core.write_outcome import RequestFailure, WriteOutcome
from canvas_mcp.tools import snapshot_attachments as tools

SELECTED = [{"assignment_id": 1, "user_id": 101, "file_id": 999}]


@pytest.fixture
async def parent(tmp_path, monkeypatch, request):
    config = SimpleNamespace(
        canvas_api_url="https://canvas.invalid/api/v1",
        canvas_api_token="SECRET",
        api_timeout=5,
    )
    monkeypatch.setattr(tools, "get_config", lambda: config)
    monkeypatch.setattr("canvas_mcp.core.record_snapshots.get_config", lambda: config)
    attachments = [
        {"id": 999, "size": 3, "content-type": "application/octet-stream"},
        {"id": 1000, "size": 3},
    ]
    attachments.append({"id": 1001, "size": 3})
    if getattr(request, "param", None) == "unknown":
        for attachment in attachments:
            attachment.pop("size", None)
    records = {
        "/courses/42": {"id": 42},
        "/courses/42/assignments": [
            {"id": 1, "course_id": 42, "points_possible": 10, "grading_type": "points"}
        ],
        "/courses/42/assignments/1/submissions": [
            {
                "id": 500,
                "assignment_id": 1,
                "user_id": 101,
                "attempt": 1,
                "workflow_state": "submitted",
                "score": None,
                "submitted_at": None,
                "late": False,
                "missing": False,
                "attachments": attachments,
            }
        ],
    }

    async def capture_request(method, path, **kwargs):
        assert method == "get"
        return records[path]

    monkeypatch.setattr(
        "canvas_mcp.core.record_snapshots.make_canvas_request", capture_request
    )
    directory = tmp_path / "parent"
    with ArtifactStore(directory, create=True) as store:
        manifest = new_manifest(
            "https://canvas.invalid",
            "42",
            "9",
            normalized_scope(["submissions"], [1], False, False, False, True),
        )
        checkpoint(store, manifest)
        await collect_pages(store, manifest, 10)
    monkeypatch.setattr(
        tools,
        "_capture_context",
        AsyncMock(return_value=("https://canvas.invalid", "42", "9")),
    )
    monkeypatch.setattr(tools, "is_http_request_active", lambda: False)
    monkeypatch.setattr(tools, "_public_ip", AsyncMock(return_value="93.184.216.34"))
    tools._GUARD.reset()
    return directory


async def fn():
    mcp = FastMCP("snapshot-attachments")
    tools.register_snapshot_attachment_tools(mcp)
    return (await mcp.get_tool("download_snapshot_attachments")).fn


class Response:
    def __init__(self, chunks=(b"abc",), status=200, headers=None):
        self.status = status
        self.headers = list((headers or {}).items())
        self.chunks = chunks

    async def aiter_stream(self):
        for chunk in self.chunks:
            if isinstance(chunk, Exception):
                raise chunk
            yield chunk


def fake_network(monkeypatch, responses):
    calls = []
    queued = list(responses)

    class Pool:
        def __init__(self, **kwargs):
            assert kwargs["retries"] == 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        @asynccontextmanager
        async def stream(self, method, url, **kwargs):
            assert method == "GET"
            kwargs["headers"] = dict(kwargs["headers"])
            calls.append((url, kwargs))
            logging.getLogger("httpcore.http11").debug(
                "Sensitive signed location Signature=SECRET"
            )
            yield queued.pop(0)

    monkeypatch.setattr(tools.httpcore, "AsyncConnectionPool", Pool)
    return calls


def submission(
    url="https://canvas.invalid/files/999/download?verifier=SECRET", second_url=None
):
    return {
        "assignment_id": 1,
        "user_id": 101,
        "attachments": [
            {"id": 999, "size": 3, "url": url, "filename": "PRIVATE original name"},
            {"id": 1000, "size": 3, "url": second_url or url},
        ],
    }


async def download(parent, tmp_path, **kwargs):
    tool = await fn()
    args = {
        "snapshot_directory": str(parent),
        "selected_files": SELECTED,
        "save_directory": str(tmp_path / "downloads"),
        **kwargs,
    }
    preview = await tool(**args)
    assert preview.get("preview"), preview
    result = await tool(**args, confirmation_token=preview["confirmation_token"])
    return preview, result


@pytest.mark.asyncio
async def test_preview_only_checks_snapshot_context_without_fetch_or_files(
    parent, tmp_path, monkeypatch
):
    request = AsyncMock()
    monkeypatch.setattr(tools, "make_canvas_request", request)
    preview = await (await fn())(str(parent), SELECTED, str(tmp_path / "downloads"))
    assert preview["nothing_stored"]
    assert not (tmp_path / "downloads").exists()
    request.assert_not_awaited()
    assert "SECRET" not in json.dumps(preview)


@pytest.mark.asyncio
async def test_canvas_redirect_s3_anonymous_pinned_private_verified_files(
    parent, tmp_path, monkeypatch, caplog
):
    monkeypatch.setattr(
        tools, "make_canvas_request", AsyncMock(return_value=submission())
    )
    calls = fake_network(
        monkeypatch,
        [
            Response(
                status=302,
                headers={
                    "Location": "https://bucket.s3.amazonaws.com/PRIVATE-file?X-Amz-Signature=SECRET"
                },
            ),
            Response(chunks=(b"a", b"bc"), headers={"Content-Length": "3"}),
        ],
    )
    caplog.set_level(logging.DEBUG, logger="httpcore.http11")
    preview, result = await download(parent, tmp_path)
    assert result["state"] == "complete", result
    assert calls[0][1]["headers"]["Authorization"] == "Bearer SECRET"
    assert "Authorization" not in calls[1][1]["headers"]
    assert calls[1][1]["headers"]["Host"] == "bucket.s3.amazonaws.com"
    assert calls[1][1]["extensions"]["sni_hostname"] == "bucket.s3.amazonaws.com"
    assert "93.184.216.34" in calls[0][0] and "93.184.216.34" in calls[1][0]
    directory = Path(result["attachment_directory"])
    with ArtifactStore(directory) as store:
        manifest = store.read_json("manifest.json")
        name = next(iter(manifest["files"]))
        assert store.read(name) == b"abc"
        assert (
            manifest["parent_checkpoint_sha256"] == preview["parent_checkpoint_sha256"]
        )
        assert manifest["files"][name]["bytes"] == 3
        text = json.dumps(manifest)
    assert (
        "PRIVATE" not in text
        and "SECRET" not in text
        and "https://" not in text.replace("https://canvas.invalid", "")
    )
    assert "SECRET" not in caplog.text and "PRIVATE" not in caplog.text
    assert directory.stat().st_mode & 0o777 == 0o700
    assert all(file.stat().st_mode & 0o777 == 0o600 for file in directory.iterdir())
    assert not any(file.name.startswith("pending-") for file in directory.iterdir())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "http://bucket.s3.amazonaws.com/file",
        "https://evil.invalid/file?Signature=SECRET",
        "https://canvas.invalid@evil.invalid/file",
        "https://127.0.0.1/file",
        "https://canvas.invalid:444/file",
        "https://canvas.invalid/file#fragment",
    ],
)
async def test_unsafe_download_destinations_send_nothing(
    parent, tmp_path, monkeypatch, url
):
    monkeypatch.setattr(
        tools, "make_canvas_request", AsyncMock(return_value=submission(url))
    )
    calls = fake_network(monkeypatch, [])
    _, result = await download(parent, tmp_path)
    assert result["state"] == "partial"
    assert result["saved_files"] == 0
    assert calls == []
    assert "SECRET" not in json.dumps(result)


@pytest.mark.asyncio
async def test_known_limits_reject_before_preview_or_requests(
    parent, tmp_path, monkeypatch
):
    request = AsyncMock()
    monkeypatch.setattr(tools, "make_canvas_request", request)
    result = await (await fn())(
        str(parent), SELECTED, str(tmp_path / "downloads"), max_file_bytes=2
    )
    assert "error" in result
    request.assert_not_awaited()
    assert not (tmp_path / "downloads").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        Response(chunks=(b"ab", b"cd")),
        Response(headers={"Content-Length": "4"}),
        Response(headers={"Content-Encoding": "gzip"}),
        Response(chunks=(b"a", TimeoutError("SECRET raw error"))),
    ],
)
async def test_oversized_encoded_interrupted_stream_has_no_partial_member(
    parent, tmp_path, monkeypatch, response
):
    monkeypatch.setattr(
        tools, "make_canvas_request", AsyncMock(return_value=submission())
    )
    calls = fake_network(monkeypatch, [response])
    _, result = await download(parent, tmp_path, max_file_bytes=3, max_total_bytes=3)
    assert result["state"] == "partial"
    directory = Path(result["attachment_directory"])
    assert not any(
        file.suffix == ".bin" or file.name.startswith("pending-")
        for file in directory.iterdir()
    )
    assert len(calls) == 1
    assert "SECRET" not in json.dumps(result)


@pytest.mark.asyncio
async def test_partial_download_preserves_first_file_and_stops_on_failure(
    parent, tmp_path, monkeypatch
):
    monkeypatch.setattr(
        tools, "make_canvas_request", AsyncMock(return_value=submission())
    )
    calls = fake_network(
        monkeypatch, [Response(), Response(chunks=(b"ab", TimeoutError("PRIVATE")))]
    )
    selected = [*SELECTED, {"assignment_id": 1, "user_id": 101, "file_id": 1000}]
    _, result = await download(parent, tmp_path, selected_files=selected)
    assert result["state"] == "partial" and result["saved_files"] == 1
    with ArtifactStore(result["attachment_directory"]) as store:
        manifest = store.read_json("manifest.json")
        assert len(manifest["files"]) == 1
        assert [item["state"] for item in manifest["items"]] == ["saved", "failed"]
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_confirmation_binds_limits_and_current_caller(
    parent, tmp_path, monkeypatch
):
    request = AsyncMock()
    monkeypatch.setattr(tools, "make_canvas_request", request)
    tool = await fn()
    args = {
        "snapshot_directory": str(parent),
        "selected_files": SELECTED,
        "save_directory": str(tmp_path / "downloads"),
    }
    preview = await tool(**args)
    result = await tool(
        **args, max_total_bytes=100, confirmation_token=preview["confirmation_token"]
    )
    assert "error" in result
    request.assert_not_awaited()
    monkeypatch.setattr(
        tools,
        "_capture_context",
        AsyncMock(return_value=("https://canvas.invalid", "42", "10")),
    )
    assert "error" in await tool(**args)


@pytest.mark.asyncio
async def test_wrong_or_unrecorded_selection_cannot_read_canvas(
    parent, tmp_path, monkeypatch
):
    request = AsyncMock()
    monkeypatch.setattr(tools, "make_canvas_request", request)
    result = await (await fn())(
        str(parent),
        [{"assignment_id": 1, "user_id": 101, "file_id": 88}],
        str(tmp_path / "downloads"),
    )
    assert "error" in result
    request.assert_not_awaited()


@pytest.mark.asyncio
async def test_revoked_permission_and_http_transport_store_nothing(
    parent, tmp_path, monkeypatch
):
    monkeypatch.setattr(
        tools, "_capture_context", AsyncMock(side_effect=ValueError("PRIVATE"))
    )
    result = await (await fn())(str(parent), SELECTED, str(tmp_path / "downloads"))
    assert "error" in result and "PRIVATE" not in json.dumps(result)
    monkeypatch.setattr(tools, "is_http_request_active", lambda: True)
    result = await (await fn())(str(parent), SELECTED, str(tmp_path / "downloads"))
    assert "stdio" in result["error"]
    assert not (tmp_path / "downloads").exists()


@pytest.mark.asyncio
async def test_too_many_redirects_are_bounded(parent, tmp_path, monkeypatch):
    monkeypatch.setattr(
        tools, "make_canvas_request", AsyncMock(return_value=submission())
    )
    calls = fake_network(
        monkeypatch,
        [Response(status=302, headers={"Location": "/redirect"}) for _ in range(4)],
    )
    _, result = await download(parent, tmp_path)
    assert result["state"] == "partial"
    assert len(calls) == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("address", ["127.0.0.1", 123])
async def test_dns_private_or_non_ip_address_rejected(monkeypatch, address):
    import asyncio
    import socket

    loop = asyncio.get_running_loop()
    monkeypatch.setattr(
        loop,
        "getaddrinfo",
        AsyncMock(
            return_value=[
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))
            ]
        ),
    )
    with pytest.raises(tools.DownloadRejected):
        await tools._public_ip("bucket.s3.amazonaws.com", 443)


@pytest.mark.asyncio
@pytest.mark.parametrize("parent", ["unknown"], indirect=True)
async def test_unknown_sizes_stream_caps_aggregate_and_stop_following_files(
    parent, tmp_path, monkeypatch
):
    response = submission()
    for item in response["attachments"]:
        item.pop("size")
    response["attachments"].append(
        {
            "id": 1001,
            "url": "https://canvas.invalid/files/1001/download?Signature=SECRET",
        }
    )
    monkeypatch.setattr(tools, "make_canvas_request", AsyncMock(return_value=response))
    calls = fake_network(monkeypatch, [Response(), Response(chunks=(b"de",))])
    selected = [
        *SELECTED,
        {"assignment_id": 1, "user_id": 101, "file_id": 1000},
        {"assignment_id": 1, "user_id": 101, "file_id": 1001},
    ]
    _, result = await download(
        parent, tmp_path, selected_files=selected, max_file_bytes=3, max_total_bytes=4
    )
    assert result["state"] == "partial"
    assert result["total_bytes"] == 3
    assert len(calls) == 2
    with ArtifactStore(result["attachment_directory"]) as store:
        manifest = store.read_json("manifest.json")
        assert [item["state"] for item in manifest["items"]] == [
            "saved",
            "rejected",
            "unattempted",
        ]
        assert len(manifest["files"]) == 1
        assert not any(name.startswith("pending-") for name in store.names())


@pytest.mark.asyncio
async def test_storage_cannot_redirect_back_to_authenticated_canvas(
    parent, tmp_path, monkeypatch
):
    monkeypatch.setattr(
        tools,
        "make_canvas_request",
        AsyncMock(
            return_value=submission(
                "https://bucket.s3.amazonaws.com/file?Signature=SECRET"
            )
        ),
    )
    calls = fake_network(
        monkeypatch,
        [
            Response(
                status=302,
                headers={
                    "Location": "https://canvas.invalid/api/v1/users/self/profile"
                },
            )
        ],
    )
    _, result = await download(parent, tmp_path)
    assert result["state"] == "partial"
    assert len(calls) == 1
    assert "Authorization" not in calls[0][1]["headers"]


@pytest.mark.asyncio
async def test_revoked_submission_permission_stops_all_later_reads(
    parent, tmp_path, monkeypatch
):
    request = AsyncMock(
        return_value=RequestFailure("PRIVATE", WriteOutcome.REJECTED, 403)
    )
    monkeypatch.setattr(tools, "make_canvas_request", request)
    calls = fake_network(monkeypatch, [])
    selected = [*SELECTED, {"assignment_id": 1, "user_id": 101, "file_id": 1000}]
    _, result = await download(parent, tmp_path, selected_files=selected)
    assert result["state"] == "partial"
    assert request.await_count == 1 and not calls
    with ArtifactStore(result["attachment_directory"]) as store:
        manifest = store.read_json("manifest.json")
        assert manifest["items"][1]["reason"] == "stopped_after_authorization_failure"


@pytest.mark.asyncio
async def test_attachment_bundle_verifier_returns_safe_summary(
    parent, tmp_path, monkeypatch
):
    monkeypatch.setattr(
        tools, "make_canvas_request", AsyncMock(return_value=submission())
    )
    fake_network(monkeypatch, [Response()])
    _, result = await download(parent, tmp_path)
    with ArtifactStore(result["attachment_directory"]) as store:
        summary = tools.verify_attachment_bundle(store)
    assert summary["integrity_verified"] and summary["state"] == "complete"
    assert summary["saved_files"] == summary["selected_files"] == 1
    assert summary["total_bytes"] == 3
    assert "https://" not in json.dumps(summary)
    assert (
        "caller_id" not in summary
        and "course_id" not in summary
        and "files" not in summary
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "corruption",
    [
        "changed_bytes",
        "missing_file",
        "scope",
        "count",
        "limits",
        "false_complete",
        "permissions",
        "parent_digest",
        "unknown_fields",
    ],
)
async def test_attachment_bundle_rejects_corruption_scope_and_missing_files(
    parent, tmp_path, monkeypatch, corruption
):
    monkeypatch.setattr(
        tools, "make_canvas_request", AsyncMock(return_value=submission())
    )
    fake_network(monkeypatch, [Response()])
    _, result = await download(parent, tmp_path)
    directory = Path(result["attachment_directory"])
    manifest = json.loads((directory / "manifest.json").read_bytes())
    filename = next(iter(manifest["files"]))
    if corruption == "changed_bytes":
        (directory / filename).write_bytes(b"xyz")
    elif corruption == "missing_file":
        (directory / filename).unlink()
    elif corruption == "permissions":
        (directory / filename).chmod(0o644)
    else:
        if corruption == "scope":
            manifest["course_id"] = "99"
        elif corruption == "count":
            manifest["total_bytes"] = 4
        elif corruption == "limits":
            manifest["limits"]["max_total_bytes"] = 2
        elif corruption == "false_complete":
            manifest["items"][0] = {
                "assignment_id": "1",
                "user_id": "101",
                "file_id": "999",
                "state": "unattempted",
                "reason": "unavailable",
            }
            manifest["files"] = {}
            manifest["total_bytes"] = 0
        elif corruption == "parent_digest":
            manifest["parent_checkpoint_sha256"] = "not-a-digest"
        else:
            manifest["url"] = "https://storage.invalid?Signature=SECRET"
        (directory / "manifest.json").write_bytes(json_bytes(manifest))
    with (
        ArtifactStore(directory) as store,
        pytest.raises(ValueError, match="verification failed"),
    ):
        tools.verify_attachment_bundle(store)


@pytest.mark.asyncio
async def test_missing_completion_marker_is_explicitly_interrupted(
    parent, tmp_path, monkeypatch
):
    monkeypatch.setattr(
        tools, "make_canvas_request", AsyncMock(return_value=submission())
    )
    fake_network(monkeypatch, [Response()])
    _, result = await download(parent, tmp_path)
    directory = Path(result["attachment_directory"])
    (directory / "manifest.json").unlink()
    with ArtifactStore(directory) as store:
        summary = tools.verify_attachment_bundle(store)
    assert summary["state"] == "interrupted"
    assert summary["saved_files"] == 1


@pytest.mark.asyncio
async def test_interrupted_unindexed_file_is_counted_without_false_completion(
    parent, tmp_path, monkeypatch
):
    monkeypatch.setattr(
        tools, "make_canvas_request", AsyncMock(return_value=submission())
    )
    fake_network(monkeypatch, [Response()])
    _, result = await download(parent, tmp_path)
    directory = Path(result["attachment_directory"])
    (directory / "manifest.json").unlink()
    (directory / "checkpoint-000002.json").unlink()
    with ArtifactStore(directory) as store:
        summary = tools.verify_attachment_bundle(store)
    assert summary["state"] == "interrupted"
    assert summary["saved_files"] == 0
    assert summary["incomplete_unindexed_files"] == 1


@pytest.mark.asyncio
async def test_partial_bundle_preserves_verified_file_count(
    parent, tmp_path, monkeypatch
):
    monkeypatch.setattr(
        tools, "make_canvas_request", AsyncMock(return_value=submission())
    )
    fake_network(
        monkeypatch, [Response(), Response(chunks=(b"a", TimeoutError("SECRET")))]
    )
    _, result = await download(
        parent,
        tmp_path,
        selected_files=[
            *SELECTED,
            {"assignment_id": 1, "user_id": 101, "file_id": 1000},
        ],
    )
    with ArtifactStore(result["attachment_directory"]) as store:
        summary = tools.verify_attachment_bundle(store)
    assert summary["state"] == "partial"
    assert summary["saved_files"] == 1 and summary["selected_files"] == 2
