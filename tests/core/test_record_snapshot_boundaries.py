import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastmcp import FastMCP

from canvas_mcp.core import record_snapshots as snapshots
from canvas_mcp.core.artifact_store import ArtifactStore, json_bytes
from canvas_mcp.core.write_outcome import RequestFailure, WriteOutcome
from canvas_mcp.tools import record_snapshots as tools


@pytest.fixture(autouse=True)
def config(monkeypatch):
    value = SimpleNamespace(
        canvas_api_url="https://canvas.invalid/api/v1", canvas_api_token="CANVAS_SECRET"
    )
    monkeypatch.setattr(snapshots, "get_config", lambda: value)
    monkeypatch.setattr(tools, "get_config", lambda: value)


def manifest(families=None, assignments=None, bodies=False):
    scope = snapshots.normalized_scope(
        families or ["course"], assignments, bodies, False, False
    )
    return snapshots.new_manifest("https://canvas.invalid", "12", "7", scope)


def add_rows(store, record_manifest, family, rows):
    projected = [
        snapshots._project(row, family, record_manifest["scope"]) for row in rows
    ]
    name = f"records-{uuid.uuid4().hex}.jsonl"
    metadata = store.write(name, b"".join(json_bytes(row) + b"\n" for row in projected))
    record_manifest["files"][name] = metadata
    unit = record_manifest["units"][family]
    unit["files"].append(name)
    unit["count"] += len(projected)
    for row in projected:
        for field in row["unavailable_fields"]:
            unit["missing_fields"][field] = unit["missing_fields"].get(field, 0) + 1
    return name


def test_complete_course_requires_one_identity_record(tmp_path):
    record_manifest = manifest()
    record_manifest["state"] = "complete"
    record_manifest["units"]["course"]["state"] = "complete"
    with ArtifactStore(tmp_path) as store:
        snapshots.checkpoint(store, record_manifest)
        with pytest.raises(ValueError, match="course unit requires identity"):
            snapshots.verify_snapshot(store)


def test_complete_assignments_require_explicit_selected_ids(tmp_path):
    record_manifest = manifest(["assignments", "submissions"], [34])
    with ArtifactStore(tmp_path) as store:
        add_rows(store, record_manifest, "course", [{"id": 12}])
        for unit in record_manifest["units"].values():
            unit["state"] = "complete"
        record_manifest["state"] = "complete"
        snapshots.checkpoint(store, record_manifest)
        with pytest.raises(ValueError, match="Missing selected assignment"):
            snapshots.verify_snapshot(store)


@pytest.mark.parametrize(
    "query",
    [
        "sig=AZURE_SECRET",
        "X-Goog-Signature=GOOGLE_SECRET",
        "X-Goog-Credential=GOOGLE_SECRET",
        "Signature=AWS_SECRET",
        "X-Amz-Signature=AWS_SECRET",
        "access_token=AUTH_SECRET",
    ],
)
def test_embedded_signed_urls_withheld_from_requested_bodies(query):
    scope = manifest(bodies=True)["scope"]
    row = snapshots._project(
        {
            "id": 12,
            "syllabus_body": f'<a href="https://storage.invalid/file?{query}">download</a>',
        },
        "course",
        scope,
    )
    assert row["syllabus_body"] is None
    assert "record.syllabus_body" in row["withheld_fields"]
    assert "SECRET" not in str(row)


def test_verified_records_consume_the_hashed_buffer(tmp_path, monkeypatch):
    record_manifest = manifest()
    with ArtifactStore(tmp_path) as store:
        name = add_rows(store, record_manifest, "course", [{"id": 12}])
        expected = record_manifest["files"][name]
        original_read = store.read

        def replace_after_read(filename):
            data = original_read(filename)
            (tmp_path / name).write_bytes(
                json_bytes({"id": 999, "unavailable_fields": []}) + b"\n"
            )
            return data

        monkeypatch.setattr(store, "read", replace_after_read)
        assert list(snapshots.verified_records(store, name, expected))[0]["id"] == 12
        with pytest.raises(ValueError, match="changed after verification"):
            list(snapshots.verified_records(store, name, expected))


def test_verifier_never_reopens_rows_through_unverified_iterator(tmp_path, monkeypatch):
    record_manifest = manifest()
    with ArtifactStore(tmp_path) as store:
        add_rows(store, record_manifest, "course", [{"id": 12}])
        record_manifest["units"]["course"]["state"] = "complete"
        record_manifest["state"] = "complete"
        snapshots.checkpoint(store, record_manifest)

        def unsafe_iterator(*args):
            raise AssertionError("Unverified second file read")

        monkeypatch.setattr(store, "records", unsafe_iterator)
        assert snapshots.verify_snapshot(store)["state"] == "complete"


def test_large_dependent_scope_remains_verifiable_and_requires_recapture(tmp_path):
    record_manifest = manifest(["assignments", "submissions", "peer_reviews"])
    with ArtifactStore(tmp_path) as store:
        add_rows(store, record_manifest, "course", [{"id": 12}])
        add_rows(
            store,
            record_manifest,
            "assignments",
            [
                {
                    "id": i,
                    "course_id": 12,
                    "points_possible": 1,
                    "grading_type": "points",
                }
                for i in range(1, 1501)
            ],
        )
        for unit in record_manifest["units"].values():
            unit["state"] = "complete"
        snapshots._dependent_units(record_manifest, store)
        record_manifest["state"] = "partial"
        snapshots.checkpoint(store, record_manifest)
        assert len(record_manifest["units"]) == 2
        assert snapshots.verify_snapshot(store)["state"] == "partial"
        summary = snapshots.snapshot_summary(record_manifest)
        assert summary["recapture_required"] is True
        assert summary["resume_possible"] is False


@pytest.mark.parametrize("status", [400, 404, 410])
async def test_expired_cursor_preserves_evidence_without_repeating_request(
    tmp_path, monkeypatch, status
):
    record_manifest = manifest(["assignments"])
    with ArtifactStore(tmp_path) as store:
        name = add_rows(store, record_manifest, "course", [{"id": 12}])
        record_manifest["units"]["course"]["state"] = "complete"
        record_manifest["units"]["assignments"].update(
            state="collecting", page="expired_cursor", seen_pages=["1"]
        )
        request = AsyncMock(
            return_value=RequestFailure("PRIVATE denial", WriteOutcome.REJECTED, status)
        )
        monkeypatch.setattr(snapshots, "make_canvas_request", request)
        result = await snapshots.collect_pages(store, record_manifest, 10)
        assert result["recapture_required"] is True
        assert result["resume_possible"] is False
        assert result["state"] == "partial"
        assert "PRIVATE" not in str(result)
        assert (
            snapshots.verify_snapshot(store)["files"][name]
            == record_manifest["files"][name]
        )
        await snapshots.collect_pages(store, record_manifest, 10)
        request.assert_awaited_once()


async def test_postallocation_failure_returns_usable_resume_directory(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(tools, "is_http_request_active", lambda: False)
    monkeypatch.setattr(
        tools,
        "_capture_context",
        AsyncMock(return_value=("https://canvas.invalid", "12", "7")),
    )
    monkeypatch.setattr(
        tools,
        "collect_pages",
        AsyncMock(side_effect=OSError("PRIVATE filesystem failure")),
    )
    tools._GUARD.reset()
    mcp = FastMCP("snapshot-boundary-test")
    tools.register_record_snapshot_tools(mcp)
    tool = (await mcp.get_tool("capture_record_snapshot")).fn
    args = {
        "course_identifier": 12,
        "families": ["course"],
        "save_directory": str(tmp_path / "snapshots"),
    }
    preview = await tool(**args)
    result = await tool(**args, confirmation_token=preview["confirmation_token"])
    assert "error" in result
    assert "PRIVATE" not in str(result)
    directory = Path(result["snapshot_directory"])
    with ArtifactStore(directory) as store:
        assert snapshots.verify_snapshot(store)["state"] == "interrupted"
