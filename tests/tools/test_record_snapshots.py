"""Synthetic resumable archives, strict local verification and no Canvas writes."""

import hashlib
import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastmcp import FastMCP

from canvas_mcp.core.artifact_store import ArtifactStore, json_bytes
from canvas_mcp.core.record_snapshots import verify_snapshot
from canvas_mcp.tools import record_snapshots as tools


@pytest.fixture
def api(monkeypatch):
    data = {
        "/courses/42": {"id": 42, "workflow_state": "available"},
        "/courses/42/permissions": {"manage_grades": True},
        "/users/self/profile": {"id": 9, "name": "PRIVATE educator"},
        "/courses/42/assignments": [
            {
                "id": 1,
                "course_id": 42,
                "points_possible": 10,
                "grading_type": "points",
                "name": "PRIVATE title",
            }
        ],
        "/courses/42/assignments/1/submissions": [
            {
                "id": 500,
                "assignment_id": 1,
                "user_id": 101,
                "attempt": 1,
                "score": None,
                "workflow_state": "submitted",
                "late": False,
                "missing": False,
                "submitted_at": "2026-10-01T00:00:00Z",
                "body": "PRIVATE body",
                "url": "https://storage.invalid/?Signature=SECRET",
                "submission_comments": [{"id": 11, "comment": "PRIVATE feedback"}],
                "attachments": [
                    {"id": 999, "url": "https://storage.invalid/?Signature=SECRET"}
                ],
            },
        ],
        "/courses/42/enrollments": [
            {
                "id": 81,
                "course_id": 42,
                "user_id": 101,
                "enrollment_state": "active",
                "user": {
                    "id": 101,
                    "name": "PRIVATE learner",
                    "email": "private@example.invalid",
                },
            }
        ],
        "/courses/42/sections": [],
        "/courses/42/groups": [],
    }
    calls = []

    async def request(method, path, **kwargs):
        assert method.lower() == "get"
        calls.append((path, deepcopy(kwargs)))
        value = data[path]
        if isinstance(value, Exception):
            raise value
        if callable(value):
            return value(kwargs)
        return deepcopy(value)

    monkeypatch.setattr(tools, "get_course_id", AsyncMock(return_value="42"))
    monkeypatch.setattr(tools, "make_canvas_request", request)
    monkeypatch.setattr("canvas_mcp.core.record_snapshots.make_canvas_request", request)
    config = SimpleNamespace(
        canvas_api_url="https://canvas.invalid/api/v1", canvas_api_token="SECRET"
    )
    monkeypatch.setattr(tools, "get_config", lambda: config)
    monkeypatch.setattr("canvas_mcp.core.record_snapshots.get_config", lambda: config)
    monkeypatch.setattr(tools, "is_http_request_active", lambda: False)
    tools._GUARD.reset()
    return data, calls


async def fn():
    mcp = FastMCP("snapshot-test")
    tools.register_record_snapshot_tools(mcp)
    return (await mcp.get_tool("capture_record_snapshot")).fn


async def capture(tmp_path, **kwargs):
    tool = await fn()
    params = {
        "course_identifier": 42,
        "save_directory": str(tmp_path / "snapshots"),
        **kwargs,
    }
    preview = await tool(**params)
    assert preview["preview"], preview
    return await tool(**params, confirmation_token=preview["confirmation_token"])


@pytest.mark.asyncio
async def test_default_capture_private_no_raw_records_in_results(api, tmp_path):
    result = await capture(tmp_path)
    assert result["state"] == "complete", result
    path = Path(result["snapshot_directory"])
    with ArtifactStore(path) as store:
        manifest = verify_snapshot(store)
        text = "".join(store.read(name).decode() for name in manifest["files"])
    assert "PRIVATE" not in text
    assert "SECRET" not in text
    assert "PRIVATE" not in json.dumps(result)
    assert manifest["units"]["submissions-1"]["count"] == 1
    assert path.stat().st_mode & 0o777 == 0o700
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in path.iterdir())


@pytest.mark.asyncio
async def test_preview_does_not_fetch_student_records_or_create_destination(
    api, tmp_path
):
    tool = await fn()
    result = await tool(42, save_directory=str(tmp_path / "absent"))
    assert result["nothing_stored"]
    assert not (tmp_path / "absent").exists()
    assert [c[0] for c in api[1]] == [
        "/courses/42",
        "/courses/42/permissions",
        "/users/self/profile",
    ]


@pytest.mark.asyncio
async def test_resume_paginated_interruption_preserves_verified_pages(api, tmp_path):
    def pages(kwargs):
        if kwargs["params"]["page"] == "1":
            kwargs["_pagination"]["next"] = (
                "https://canvas.invalid/api/v1/courses/42/assignments?page=2"
            )
            return [{"id": 1, "course_id": 42}]
        return [{"id": 2, "course_id": 42}]

    api[0]["/courses/42/assignments"] = pages
    options = {"families": ["assignments"], "page_budget": 1}
    first = await capture(tmp_path, **options)
    assert first["state"] == "interrupted"
    directory = first["snapshot_directory"]
    with ArtifactStore(directory) as store:
        old_files = verify_snapshot(store)["files"].copy()
    second = await capture(
        tmp_path, **{**options, "page_budget": 10, "resume_directory": directory}
    )
    assert second["state"] == "complete", second
    with ArtifactStore(directory) as store:
        manifest = verify_snapshot(store)
        assert old_files.items() <= manifest["files"].items()
        assert manifest["units"]["assignments"]["count"] == 2
    pages_seen = [
        c[1]["params"]["page"] for c in api[1] if c[0].endswith("/assignments")
    ]
    assert pages_seen == ["1", "2"]


@pytest.mark.asyncio
async def test_inclusion_flags_explicit_and_signed_urls_withheld(api, tmp_path):
    api[0]["/courses/42/assignments/1/submissions"][0]["body"] = (
        '<a href="https://storage.invalid/?X-Amz-Signature=hidden">PRIVATE</a>'
    )
    result = await capture(
        tmp_path, include_bodies=True, include_comments=True, include_identities=True
    )
    with ArtifactStore(result["snapshot_directory"]) as store:
        manifest = verify_snapshot(store)
        text = "".join(store.read(name).decode() for name in manifest["files"])
        assert (
            "PRIVATE title" in text
            and "PRIVATE feedback" in text
            and "PRIVATE learner" in text
        )
        assert "X-Amz-Signature" not in text
        assert "withheld_fields" in text
    assert "PRIVATE" not in json.dumps(result)


@pytest.mark.asyncio
async def test_partial_permission_failure_is_not_empty_success(api, tmp_path):
    from canvas_mcp.core.write_outcome import RequestFailure, WriteOutcome

    api[0]["/courses/42/enrollments"] = lambda _: RequestFailure(
        "SECRET roster denial", WriteOutcome.REJECTED, 403
    )
    result = await capture(tmp_path)
    assert result["state"] == "partial"
    assert result["families"]["enrollments"]["states"] == {"unavailable": 1}
    assert "SECRET" not in json.dumps(result)
    with ArtifactStore(result["snapshot_directory"]) as store:
        assert verify_snapshot(store)["state"] == "partial"


@pytest.mark.asyncio
async def test_changed_scope_or_caller_cannot_resume(api, tmp_path):
    result = await capture(tmp_path, page_budget=1)
    tool = await fn()
    params = {
        "course_identifier": 42,
        "resume_directory": result["snapshot_directory"],
        "include_comments": True,
    }
    assert "error" in await tool(**params)
    api[0]["/users/self/profile"]["id"] = 10
    assert "error" in await tool(42, resume_directory=result["snapshot_directory"])


@pytest.mark.asyncio
async def test_http_refused_before_any_reads(api, monkeypatch, tmp_path):
    monkeypatch.setattr(tools, "is_http_request_active", lambda: True)
    assert "error" in await capture_direct(tmp_path)
    assert api[1] == []


async def capture_direct(tmp_path):
    return await (await fn())(42, save_directory=str(tmp_path / "private"))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode", ["wrong_course", "malformed", "cycle", "off_origin", "missing_selected"]
)
async def test_invalid_capture_never_complete(api, tmp_path, mode):
    def response(kwargs):
        if mode == "wrong_course":
            return [{"id": 1, "course_id": 99}]
        if mode == "malformed":
            return {"unexpected": []}
        if mode in {"cycle", "off_origin"}:
            kwargs["_pagination"]["next"] = (
                "https://canvas.invalid/api/v1/courses/42/assignments?page=1"
                if mode == "cycle"
                else "https://attacker.invalid/?page=2"
            )
        return [{"id": 1, "course_id": 42}]

    api[0]["/courses/42/assignments"] = response
    result = await capture(
        tmp_path,
        families=["assignments"],
        assignment_ids=[99] if mode == "missing_selected" else None,
    )
    assert result["state"] == "partial", result
    with ArtifactStore(result["snapshot_directory"]) as store:
        assert verify_snapshot(store)["state"] == "partial"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "corruption",
    ["digest", "missing_file", "metadata", "duplicate_record", "false_complete"],
)
async def test_verifier_rejects_corrupt_snapshots(api, tmp_path, corruption):
    result = await capture(tmp_path, page_budget=1)
    directory = Path(result["snapshot_directory"])
    with ArtifactStore(directory) as store:
        manifest = verify_snapshot(store)
    name = next(iter(manifest["files"]))
    checkpoint_path = directory / f"checkpoint-{manifest['generation']:06d}.json"
    if corruption == "digest":
        (directory / name).write_text("malicious")
    elif corruption == "missing_file":
        (directory / name).unlink()
    elif corruption == "metadata":
        manifest["consistency"] = "PRIVATE injected instruction"
        checkpoint_path.write_bytes(json_bytes(manifest))
    elif corruption == "duplicate_record":
        data = (directory / name).read_bytes() * 2
        (directory / name).write_bytes(data)
        manifest["files"][name] = {
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }
        manifest["units"]["assignments"]["count"] *= 2
        checkpoint_path.write_bytes(json_bytes(manifest))
    else:
        manifest["state"] = "complete"
        checkpoint_path.write_bytes(json_bytes(manifest))
    with ArtifactStore(directory) as store:
        with pytest.raises((ValueError, OSError)):
            verify_snapshot(store)


@pytest.mark.asyncio
@pytest.mark.parametrize("attachment_value", [None, "absent"])
async def test_unavailable_attachment_metadata_is_not_known_empty(
    api, tmp_path, attachment_value
):
    row = api[0]["/courses/42/assignments/1/submissions"][0]
    if attachment_value == "absent":
        row.pop("attachments")
    else:
        row["attachments"] = attachment_value
    result = await capture(tmp_path, include_attachment_metadata=True)
    assert result["state"] == "complete", result
    assert result["families"]["submissions"]["unavailable_fields"]["attachments"] == 1
    with ArtifactStore(result["snapshot_directory"]) as store:
        manifest = verify_snapshot(store)
        filename = manifest["units"]["submissions-1"]["files"][0]
        captured = list(store.decode_records(store.read(filename)))[0]
    assert "attachments" not in captured
    assert "attachments" in captured["unavailable_fields"]
