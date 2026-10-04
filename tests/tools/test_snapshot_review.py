"""Synthetic local archive verification, coverage and identity selection."""

import csv
import hashlib
import io
import json
import os
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastmcp import FastMCP

from canvas_mcp.core.artifact_store import ArtifactStore, json_bytes
from canvas_mcp.core.local_artifacts import write_private_bundle
from canvas_mcp.core.record_snapshots import checkpoint, new_manifest, normalized_scope
from canvas_mcp.tools import snapshot_review as review

NOW = datetime(2026, 10, 4, 12, tzinfo=UTC)
ORIGIN = "https://canvas.invalid"


@pytest.fixture
def tools(monkeypatch):
    captured = {}
    server = FastMCP("snapshot-review-test")
    original = server.tool

    def capture(*args, **kwargs):
        decorate = original(*args, **kwargs)

        def register(function):
            captured[function.__name__] = function
            return decorate(function)

        return register

    monkeypatch.setattr(server, "tool", capture)
    monkeypatch.setattr(review, "_now", lambda: NOW)
    monkeypatch.setattr(
        review, "get_config", lambda: SimpleNamespace(canvas_api_url=ORIGIN + "/api/v1")
    )
    monkeypatch.setattr(review, "is_http_request_active", lambda: False)
    denied = AsyncMock(side_effect=AssertionError("No Canvas requests permitted"))
    monkeypatch.setattr("canvas_mcp.core.client.make_canvas_request", denied)
    monkeypatch.setattr("canvas_mcp.core.client.fetch_all_paginated_results", denied)
    review.register_snapshot_review_tools(server)
    yield captured
    denied.assert_not_called()


def snapshot(
    path,
    assignments=None,
    *,
    families=None,
    states=None,
    metadata=None,
    include_bodies=False,
    course_id="42",
    include_attachment_metadata=False,
    submissions=None,
):
    rows = {"course": [{"id": int(course_id), "unavailable_fields": []}]}
    if assignments is not None:
        rows["assignments"] = [
            {
                "course_id": int(course_id),
                "points_possible": 10,
                "grading_type": "points",
                "unavailable_fields": [],
                **row,
            }
            for row in assignments
        ]
    scope = normalized_scope(
        families or list(rows),
        None,
        include_bodies,
        False,
        False,
        include_attachment_metadata,
    )
    manifest = new_manifest(ORIGIN, course_id, "900", scope)
    states = states or {}
    with ArtifactStore(path, create=True) as store:
        for family, unit in manifest["units"].items():
            records = rows.get(family, [])
            if records:
                name = f"records-{uuid.uuid4().hex}.jsonl"
                manifest["files"][name] = store.write(
                    name, b"".join(json_bytes(record) + b"\n" for record in records)
                )
                unit["files"].append(name)
            unit["count"] = len(records)
            unit["state"] = states.get(family, "complete")
        if (
            "submissions" in scope["families"]
            and manifest["units"]["assignments"]["state"] == "complete"
        ):
            for row in rows["assignments"]:
                manifest["units"][f"submissions-{row['id']}"] = {
                    "family": "submissions",
                    "assignment_id": str(row["id"]),
                    "state": states.get("submissions", "complete"),
                    "page": "1",
                    "files": [],
                    "count": 0,
                    "missing_fields": {},
                    "seen_pages": [],
                }
                unit = manifest["units"][f"submissions-{row['id']}"]
                selected = [
                    {
                        "attempt": 1,
                        "workflow_state": "submitted",
                        "score": None,
                        "submitted_at": None,
                        "late": False,
                        "missing": False,
                        "unavailable_fields": [],
                        **record,
                    }
                    for record in submissions or []
                    if str(record["assignment_id"]) == str(row["id"])
                ]
                if selected:
                    name = f"records-{uuid.uuid4().hex}.jsonl"
                    manifest["files"][name] = store.write(
                        name,
                        b"".join(json_bytes(record) + b"\n" for record in selected),
                    )
                    unit["files"].append(name)
                    unit["count"] = len(selected)
                    for record in selected:
                        for field in record["unavailable_fields"]:
                            unit["missing_fields"][field] = (
                                unit["missing_fields"].get(field, 0) + 1
                            )
        manifest["state"] = (
            "complete"
            if all(unit["state"] == "complete" for unit in manifest["units"].values())
            else "partial"
        )
        manifest.update(metadata or {})
        checkpoint(store, manifest)
    return path


def pseudonym(real_id):
    return "Student_" + hashlib.sha256(str(real_id).encode()).hexdigest()[:8]


def identity_map(path, *, records=None, metadata=None):
    if records is None:
        records = [
            {
                "real_id": 101,
                "anonymous_id": pseudonym(101),
                "real_name": "=PRIVATE_ONE()",
                "real_email": "+private1@example.invalid",
            },
            {
                "real_id": 102,
                "anonymous_id": pseudonym(102),
                "real_name": "PRIVATE_TWO",
                "real_email": None,
            },
        ]
    output = io.StringIO(newline="")
    writer = csv.DictWriter(
        output, fieldnames=["real_name", "real_id", "real_email", "anonymous_id"]
    )
    writer.writeheader()
    writer.writerows(records)
    info = {
        "kind": "student_identity_map",
        "schema_version": 1,
        "canvas_origin": ORIGIN,
        "course_id": "42",
        "pseudonym_algorithm": "sha256-id8-student-v1",
        "created_at": (NOW - timedelta(hours=1)).isoformat(),
        "record_count": len(records),
    }
    info.update(metadata or {})
    return write_private_bundle(
        str(path),
        "synthetic-map",
        {
            "identities.json": json_bytes(records),
            "anonymization_map.csv": output.getvalue().encode(),
        },
        info,
    )


@pytest.mark.asyncio
async def test_verifier_returns_only_counts_and_preserves_archive(tools, tmp_path):
    archive = snapshot(
        tmp_path / "records",
        [{"id": 34, "name": "PRIVATE_RECORD_NAME"}],
        include_bodies=True,
    )
    before = {path.name: path.read_bytes() for path in archive.iterdir()}
    result = await tools["verify_record_snapshot"](str(archive))
    assert result["status"] == "verified" and result["state"] == "complete"
    assert result["families"]["assignments"]["records"] == 1
    assert "PRIVATE_RECORD_NAME" not in json.dumps(result)
    assert "course_id" not in result and "caller_id" not in result
    assert before == {path.name: path.read_bytes() for path in archive.iterdir()}


@pytest.mark.asyncio
async def test_complete_comparison_counts_changes_without_record_values(
    tools, tmp_path
):
    before = snapshot(tmp_path / "before", [{"id": 34}, {"id": 35}])
    after = snapshot(
        tmp_path / "after", [{"id": 34, "points_possible": 20}, {"id": 36}]
    )
    result = await tools["compare_record_snapshots"](str(before), str(after))
    assert result["status"] == "compared"
    family = result["families"]["assignments"]
    assert (family["added"], family["removed"], family["changed"]) == (1, 1, 1)
    assert family["coverage_complete"]
    assert "user_id" not in json.dumps(result) and "records-" not in json.dumps(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["collecting", "failed", "unavailable"])
async def test_partial_comparison_never_turns_unobserved_records_into_removals(
    tools, tmp_path, state
):
    before = snapshot(tmp_path / "before", [{"id": 34}, {"id": 35}])
    after = snapshot(
        tmp_path / "after",
        [{"id": 34, "points_possible": 20}],
        states={"assignments": state},
    )
    result = await tools["compare_record_snapshots"](str(before), str(after))
    family = result["families"]["assignments"]
    assert family["added"] is None and family["removed"] is None
    assert family["changed"] == 1 and not family["coverage_complete"]
    assert family["unavailable"]["after_units"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("unavailable", [False, True])
async def test_empty_dependent_family_distinguishes_empty_assignment_scope_from_denial(
    tools, tmp_path, unavailable
):
    options = {
        "families": ["course", "assignments", "submissions"],
        "states": {"assignments": "unavailable"} if unavailable else {},
    }
    before = snapshot(tmp_path / "before", [], **options)
    after = snapshot(tmp_path / "after", [], **options)
    result = await tools["compare_record_snapshots"](str(before), str(after))
    family = result["families"]["submissions"]
    assert family["coverage_complete"] is (not unavailable)
    assert family["removed"] == (None if unavailable else 0)
    assert family["unavailable"]["after_units"] == (1 if unavailable else 0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"canvas_origin": "https://other.invalid"},
        {"caller_id": "901"},
        {
            "scope": normalized_scope(
                ["course", "assignments"], None, True, False, False
            )
        },
    ],
)
async def test_comparison_rejects_different_origin_caller_or_scope(
    tools, tmp_path, change
):
    before = snapshot(tmp_path / "before", [{"id": 34}])
    after = snapshot(tmp_path / "after", [{"id": 34}], metadata=change)
    result = await tools["compare_record_snapshots"](str(before), str(after))
    assert result["status"] == "rejected" and "different" in result["error"]


@pytest.mark.asyncio
async def test_tampered_snapshot_fails_without_contents_or_exception_text(
    tools, tmp_path
):
    archive = snapshot(tmp_path / "records", [{"id": 34}])
    record = next(archive.glob("records-*.jsonl"))
    record.write_text("PRIVATE_TAMPERED_DATA")
    result = await tools["verify_record_snapshot"](str(archive))
    assert result["status"] == "rejected"
    assert "PRIVATE" not in json.dumps(result)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name,args",
    [
        ("verify_record_snapshot", ["unopened"]),
        ("compare_record_snapshots", ["unopened", "unopened"]),
        ("lookup_student_identities", ["unopened", [pseudonym(101)], 42]),
    ],
)
async def test_http_requests_rejected_before_local_file_access(
    tools, monkeypatch, name, args
):
    monkeypatch.setattr(review, "is_http_request_active", lambda: True)
    monkeypatch.setattr(
        review,
        "ArtifactStore",
        lambda *args, **kwargs: pytest.fail("HTTP touched local files"),
    )
    result = await tools[name](*args)
    assert result["status"] == "rejected" and "stdio" in result["error"]


@pytest.mark.asyncio
async def test_identity_lookup_saves_only_selected_private_raw_and_safe_csv(
    tools, tmp_path, caplog
):
    source = identity_map(tmp_path / "maps")
    original = (source / "identities.json").read_bytes()
    result = await tools["lookup_student_identities"](
        str(source), [pseudonym(101)], "042", str(tmp_path / "selections")
    )
    assert result["status"] == "saved" and result["selected_count"] == 1
    selected = review.ArtifactStore(result["bundle_path"])
    with selected:
        records = selected.read_json("identities.json")
        assert records[0]["real_name"] == "=PRIVATE_ONE()" and len(records) == 1
        assert "'=PRIVATE_ONE()" in selected.read("anonymization_map.csv").decode()
        assert (
            "'+private1@example.invalid"
            in selected.read("anonymization_map.csv").decode()
        )
        assert (
            selected.read_json("manifest.json")["kind"] == "student_identity_selection"
        )
    assert original == (source / "identities.json").read_bytes()
    assert "PRIVATE" not in json.dumps(result) + caplog.text
    assert "private1@example" not in json.dumps(result) + caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "metadata",
    [
        {"canvas_origin": "https://other.invalid"},
        {"course_id": "43"},
        {"schema_version": 2},
        {"pseudonym_algorithm": "unknown"},
        {"created_at": (NOW - timedelta(days=8)).isoformat()},
        {"created_at": (NOW + timedelta(minutes=1)).isoformat()},
        {"created_at": "2026-10-04T11:00:00"},
        {"record_count": 3},
    ],
)
async def test_identity_map_scope_schema_age_and_counts_fail_closed(
    tools, tmp_path, metadata
):
    source = identity_map(tmp_path / "maps", metadata=metadata)
    destination = tmp_path / "selection"
    result = await tools["lookup_student_identities"](
        str(source), [pseudonym(101)], 42, str(destination)
    )
    assert result["status"] == "rejected" and not destination.exists()
    assert "PRIVATE" not in json.dumps(result)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "records",
    [
        [
            {
                "real_id": True,
                "anonymous_id": pseudonym(1),
                "real_name": "PRIVATE",
                "real_email": None,
            }
        ],
        [
            {
                "real_id": 101,
                "anonymous_id": "Student_deadbeef",
                "real_name": "PRIVATE",
                "real_email": None,
            }
        ],
        [
            {
                "real_id": 101,
                "anonymous_id": pseudonym(101),
                "real_name": "PRIVATE",
                "real_email": None,
            }
        ]
        * 2,
    ],
)
async def test_invalid_or_duplicate_map_records_do_not_create_selection(
    tools, tmp_path, records
):
    source = identity_map(tmp_path / "maps", records=records)
    result = await tools["lookup_student_identities"](
        str(source), [pseudonym(101)], 42, str(tmp_path / "selection")
    )
    assert result["status"] == "rejected" and not (tmp_path / "selection").exists()
    assert "PRIVATE" not in json.dumps(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["content", "permissions", "symlink", "hardlink"])
async def test_identity_map_private_file_and_integrity_boundary(
    tools, tmp_path, damage
):
    source = identity_map(tmp_path / "maps")
    target = source / "identities.json"
    if damage == "content":
        target.write_text("PRIVATE_TAMPER")
    elif damage == "permissions":
        target.chmod(0o644)
    elif damage == "symlink":
        real = source / "raw.json"
        target.rename(real)
        target.symlink_to(real)
    else:
        os.link(target, source / "raw.json")
    result = await tools["lookup_student_identities"](
        str(source), [pseudonym(101)], 42, str(tmp_path / "selection")
    )
    assert result["status"] == "rejected" and not (tmp_path / "selection").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "selected,course",
    [
        ([pseudonym(999)], 42),
        ([pseudonym(101)] * 2, 42),
        ([f"Student_{n:08x}" for n in range(51)], 42),
        ([pseudonym(101)], "SYNTH/42"),
    ],
)
async def test_unmatched_duplicate_oversized_or_non_numeric_selection_rejected(
    tools, tmp_path, selected, course
):
    source = identity_map(tmp_path / "maps")
    result = await tools["lookup_student_identities"](
        str(source), selected, course, str(tmp_path / "selection")
    )
    assert result["status"] == "rejected" and not (tmp_path / "selection").exists()


@pytest.mark.asyncio
async def test_selection_storage_failure_is_unconfirmed_and_redacted(
    tools, tmp_path, monkeypatch
):
    source = identity_map(tmp_path / "maps")

    def fail(*args, **kwargs):
        raise OSError("PRIVATE_EXCEPTION")

    monkeypatch.setattr(review, "write_private_bundle", fail)
    result = await tools["lookup_student_identities"](
        str(source), [pseudonym(101)], 42, str(tmp_path / "selection")
    )
    assert result["status"] == "unconfirmed" and "PRIVATE" not in json.dumps(result)


@pytest.mark.asyncio
async def test_comparison_rejects_valid_other_course_and_detects_consistency_uncertainty(
    tools, tmp_path
):
    before = snapshot(tmp_path / "before", [{"id": 34}, {"id": 35}])
    other = snapshot(tmp_path / "other", [{"id": 34}], course_id="43")
    rejected = await tools["compare_record_snapshots"](str(before), str(other))
    assert rejected["status"] == "rejected" and "different" in rejected["error"]
    changed = snapshot(
        tmp_path / "changed",
        [{"id": 34}],
        metadata={"consistency_issues": ["assignments"], "state": "partial"},
    )
    result = await tools["compare_record_snapshots"](str(before), str(changed))
    assert result["families"]["assignments"]["removed"] is None
    assert result["families"]["assignments"]["unavailable"]["after_units"] == 1


@pytest.mark.asyncio
async def test_review_waits_for_completed_writer_and_same_archive_can_compare(
    tools, tmp_path
):
    archive = snapshot(tmp_path / "records", [{"id": 34}])
    with ArtifactStore(archive) as writer:
        writer.lock()
        result = await tools["verify_record_snapshot"](str(archive))
        assert result["status"] == "rejected"
    result = await tools["compare_record_snapshots"](str(archive), str(archive))
    assert result["status"] == "compared"
    assert all(
        family["added"] == family["removed"] == family["changed"] == 0
        for family in result["families"].values()
    )


@pytest.mark.asyncio
async def test_comparison_rehashes_record_buffer_after_initial_verification(
    tools, tmp_path, monkeypatch
):
    before = snapshot(tmp_path / "before", [{"id": 34}])
    after = snapshot(tmp_path / "after", [{"id": 34}])
    original = review.verify_snapshot

    def changed(store):
        manifest = original(store)
        if store.path == before:
            filename = manifest["units"]["assignments"]["files"][0]
            (before / filename).write_text("PRIVATE_CHANGED_AFTER_VERIFY")
        return manifest

    monkeypatch.setattr(review, "verify_snapshot", changed)
    result = await tools["compare_record_snapshots"](str(before), str(after))
    assert result["status"] == "rejected"
    assert "PRIVATE" not in json.dumps(result)


@pytest.mark.asyncio
async def test_assignment_consistency_issue_keeps_dependent_family_unknown(
    tools, tmp_path
):
    options = {
        "families": ["course", "assignments", "submissions"],
        "metadata": {"consistency_issues": ["assignments"], "state": "partial"},
    }
    before = snapshot(tmp_path / "before", [{"id": 34}], **options)
    after = snapshot(tmp_path / "after", [{"id": 34}], **options)
    verified = await tools["verify_record_snapshot"](str(after))
    assert not verified["families"]["submissions"]["coverage_complete"]
    result = await tools["compare_record_snapshots"](str(before), str(after))
    assert result["families"]["submissions"]["removed"] is None


@pytest.mark.asyncio
async def test_explicit_age_override_can_accept_older_map_without_reusing_selection_as_map(
    tools, tmp_path
):
    source = identity_map(
        tmp_path / "maps",
        metadata={"created_at": (NOW - timedelta(days=8)).isoformat()},
    )
    result = await tools["lookup_student_identities"](
        str(source), [pseudonym(101)], 42, str(tmp_path / "selection"), max_age_days=9
    )
    assert result["status"] == "saved"
    retry = await tools["lookup_student_identities"](
        result["bundle_path"],
        [pseudonym(101)],
        42,
        str(tmp_path / "other"),
        max_age_days=9,
    )
    assert retry["status"] == "rejected" and not (tmp_path / "other").exists()


@pytest.mark.asyncio
async def test_attachment_metadata_scope_compares_hashes_without_returning_metadata(
    tools, tmp_path
):
    options = {
        "families": ["course", "assignments", "submissions"],
        "include_attachment_metadata": True,
    }
    record = {
        "assignment_id": 34,
        "user_id": 101,
        "attachments": [{"id": 88, "size": 500, "content-type": "application/pdf"}],
    }
    before = snapshot(
        tmp_path / "before", [{"id": 34}], submissions=[record], **options
    )
    changed = {
        **record,
        "attachments": [{"id": 88, "size": 600, "content-type": "application/pdf"}],
    }
    after = snapshot(tmp_path / "after", [{"id": 34}], submissions=[changed], **options)
    verified = await tools["verify_record_snapshot"](str(before))
    assert verified["status"] == "verified"
    result = await tools["compare_record_snapshots"](str(before), str(after))
    assert result["families"]["submissions"]["changed"] == 1
    assert "application/pdf" not in json.dumps(
        result
    ) and "attachments" not in json.dumps(result)
    excluded = snapshot(
        tmp_path / "excluded", [{"id": 34}], families=options["families"]
    )
    rejected = await tools["compare_record_snapshots"](str(before), str(excluded))
    assert rejected["status"] == "rejected" and "different" in rejected["error"]


@pytest.mark.asyncio
async def test_attachment_metadata_unavailable_is_preserved_in_safe_summary(
    tools, tmp_path
):
    archive = snapshot(
        tmp_path / "archive",
        [{"id": 34}],
        families=["course", "assignments", "submissions"],
        include_attachment_metadata=True,
        submissions=[
            {
                "assignment_id": 34,
                "user_id": 101,
                "unavailable_fields": ["attachments"],
            }
        ],
    )
    result = await tools["verify_record_snapshot"](str(archive))
    assert result["status"] == "verified"
    assert result["families"]["submissions"]["unavailable_fields"] == {
        "attachments": 1
    }
    assert "101" not in json.dumps(result)


@pytest.mark.asyncio
async def test_expired_cursor_summary_requires_recapture_instead_of_resume(
    tools, tmp_path
):
    archive = snapshot(
        tmp_path / "records", [{"id": 34}], states={"assignments": "failed"}
    )
    with ArtifactStore(archive) as store:
        manifest = store.read_json("checkpoint-000001.json")
        manifest["units"]["assignments"]["error"] = "cursor_expired"
        checkpoint(store, manifest)
    result = await tools["verify_record_snapshot"](str(archive))
    assert result["status"] == "verified" and result["state"] == "partial"
    assert not result["resume_possible"] and result["recapture_required"]


@pytest.mark.asyncio
@pytest.mark.parametrize("finished", [True, False])
async def test_attachment_verification_dispatches_over_mcp_without_raw_data(
    tools, tmp_path, finished
):
    from copy import deepcopy

    from fastmcp import Client

    archive = tmp_path / "attachments"
    selected = {
        "assignment_id": "34",
        "user_id": "101",
        "file_id": "88",
        "expected_bytes": 14,
    }
    manifest = {
        "schema_version": 1,
        "kind": "snapshot_attachment_download",
        "canvas_origin": ORIGIN,
        "course_id": "42",
        "caller_id": "900",
        "parent_checkpoint_sha256": "a" * 64,
        "started_at": NOW.isoformat(),
        "observed_until": NOW.isoformat(),
        "state": "interrupted",
        "selections": [selected],
        "items": [],
        "files": {},
        "canvas_writes": 0,
        "limits": {"max_file_bytes": 14, "max_total_bytes": 14},
        "total_bytes": 0,
    }
    with ArtifactStore(archive, create=True) as store:
        store.write("checkpoint-000001.json", json_bytes(manifest))
        filename = "attachment-88-" + "b" * 32 + ".bin"
        metadata = store.write(filename, b"PRIVATE_BINARY")
        manifest["items"] = [
            {key: selected[key] for key in ("assignment_id", "user_id", "file_id")}
            | {"state": "saved", "artifact": filename, **metadata}
        ]
        manifest["files"] = {filename: metadata}
        manifest["total_bytes"] = metadata["bytes"]
        store.write("checkpoint-000002.json", json_bytes(manifest))
        if finished:
            completed = deepcopy(manifest)
            completed["state"] = "complete"
            store.write("manifest.json", json_bytes(completed))
    server = FastMCP("attachment-verification-dispatch")
    review.register_snapshot_review_tools(server)
    async with Client(server) as mcp:
        response = await mcp.call_tool(
            "verify_record_snapshot", {"snapshot_directory": str(archive)}
        )
    result = json.loads(response.content[0].text)
    assert (
        result["status"] == "verified"
        and result["kind"] == "snapshot_attachment_download"
    )
    assert result["state"] == ("complete" if finished else "interrupted")
    assert result["saved_files"] == result["selected_files"] == 1
    assert result["integrity_verified"]
    assert "PRIVATE" not in json.dumps(result) and "attachment-88" not in json.dumps(
        result
    )
    assert (
        not {"course_id", "caller_id", "user_id", "file_id", "items", "files"}
        & result.keys()
    )
    rejected = await tools["compare_record_snapshots"](str(archive), str(archive))
    assert rejected["status"] == "rejected"
