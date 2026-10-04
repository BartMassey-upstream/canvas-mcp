"""Synthetic reviewed-attempt and criterion-persistence boundaries."""

import json
from unittest.mock import AsyncMock, patch

import pytest
from fastmcp import FastMCP

from canvas_mcp.tools.assignments import register_educator_assignment_tools


async def tool():
    mcp = FastMCP("attempt-boundaries")
    register_educator_assignment_tools(mcp)
    return next(
        t.fn for t in await mcp.list_tools() if t.name == "bulk_grade_submissions"
    )


def submission(**fields):
    return dict(
        assignment_id=12,
        user_id=3,
        attempt=1,
        score=8,
        grade="8",
        grade_matches_current_submission=True,
        submission_comments=[],
        **fields,
    )


def item(result):
    return json.loads(result.split("Recovery data (JSON):\n")[1])["items"][0]


@pytest.fixture(autouse=True)
def course():
    with (
        patch("canvas_mcp.tools.assignments.get_course_id", AsyncMock(return_value=42)),
        patch(
            "canvas_mcp.tools.assignments.get_course_code",
            AsyncMock(return_value="Synthetic"),
        ),
    ):
        yield


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "expected", [True, -1, 1.0, "1", float("nan"), float("inf"), 10**1000, [], {}]
)
async def test_invalid_expected_attempt_rejects_whole_batch_before_reads(expected):
    mock = AsyncMock()
    with patch("canvas_mcp.tools.assignments.make_canvas_request", mock):
        result = await (await tool())(
            42, 12, {"3": {"grade": 8}, "4": {"grade": 9, "expected_attempt": expected}}
        )
    assert "expected_attempt" in result
    mock.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("expected,observed", [(None, 1), (1, 2), (0, 1), (1, None)])
async def test_changed_reviewed_attempt_never_dispatches(expected, observed):
    before = submission()
    before.update(attempt=observed, workflow_state="unsubmitted", submitted_at=None)
    mock = AsyncMock(return_value=before)
    with patch("canvas_mcp.tools.assignments.make_canvas_request", mock):
        result = await (await tool())(
            42, 12, {"3": {"grade": 8, "expected_attempt": expected}}
        )
    assert item(result)["reason"] == "reviewed_attempt_changed"
    assert item(result)["expected_attempt"] == expected
    assert item(result)["outcome"] == "unattempted"
    assert [c.args[0] for c in mock.await_args_list] == ["get"]


@pytest.mark.asyncio
@pytest.mark.parametrize("expected", [None, 0, 1])
async def test_dry_run_preserves_reviewed_attempt_without_reads(expected):
    mock = AsyncMock()
    with patch("canvas_mcp.tools.assignments.make_canvas_request", mock):
        result = await (await tool())(
            42, 12, {"3": {"grade": 8, "expected_attempt": expected}}, dry_run=True
        )
    assert item(result)["expected_attempt"] == expected
    mock.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("bound", [False, True])
async def test_never_submitted_null_attempt_can_grade_and_verify(bound):
    before = submission()
    before.update(
        attempt=None,
        workflow_state="unsubmitted",
        submitted_at=None,
        grade=None,
        score=None,
    )
    after = submission()
    after.update(attempt=None, workflow_state="graded", submitted_at=None)
    mock = AsyncMock(side_effect=[before, {}, after])
    proposal = {"grade": 8, **({"expected_attempt": None} if bound else {})}
    with patch("canvas_mcp.tools.assignments.make_canvas_request", mock):
        result = await (await tool())(42, 12, {"3": proposal})
    assert item(result)["outcome"] == "verified"
    assert [c.args[0] for c in mock.await_args_list] == ["get", "put", "get"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid",
    [
        {},
        {"attempt": True},
        {"attempt": -1},
        {"attempt": "1"},
        {"attempt": None},
        {"attempt": None, "workflow_state": "submitted", "submitted_at": None},
        {
            "attempt": None,
            "workflow_state": "unsubmitted",
            "submitted_at": "2026-01-01T00:00:00Z",
        },
        {"attempt": None, "workflow_state": "unsubmitted"},
    ],
)
async def test_missing_or_unproven_attempt_never_dispatches(invalid):
    before = submission()
    before.pop("attempt")
    before.update(invalid)
    mock = AsyncMock(return_value=before)
    with patch("canvas_mcp.tools.assignments.make_canvas_request", mock):
        result = await (await tool())(42, 12, {"3": {"grade": 8}})
    assert item(result)["reason"] == "prewrite_attempt_unavailable"
    assert mock.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", ["missing_attempt", "timestamp", "state", "new_attempt"]
)
async def test_null_attempt_readback_requires_same_never_submitted_evidence(change):
    before = submission()
    before.update(attempt=None, workflow_state="unsubmitted", submitted_at=None)
    after = dict(before)
    after["workflow_state"] = "graded"
    if change == "missing_attempt":
        after.pop("attempt")
    elif change == "timestamp":
        after["submitted_at"] = "2026-01-01T00:00:00Z"
    elif change == "state":
        after["workflow_state"] = "pending_review"
    else:
        after["attempt"] = 1
    mock = AsyncMock(side_effect=[before, {}, after])
    with patch("canvas_mcp.tools.assignments.make_canvas_request", mock):
        result = await (await tool())(
            42, 12, {"3": {"grade": 8, "expected_attempt": None}}
        )
    assert item(result)["outcome"] == "unknown"
    assert item(result)["reason"] == "submission_attempt_changed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("comments", "Stale"),
        ("comments", None),
        ("rating_id", "wrong"),
        ("rating_id", None),
        ("comments", "Requested"),
    ],
)
async def test_matching_rubric_points_cannot_hide_failed_feedback_or_rating(
    field, value
):
    assessment = {"a": {"points": 8, "rating_id": "good", "comments": "Requested"}}
    persisted = {"a": dict(assessment["a"])}
    persisted["a"][field] = value
    after = submission(rubric_assessment=persisted)
    assignment = {"id": 12, "use_rubric_for_grading": True, "rubric": [{"id": "a"}]}
    mock = AsyncMock(side_effect=[assignment, submission(), {}, after])
    with patch("canvas_mcp.tools.assignments.make_canvas_request", mock):
        result = await (await tool())(
            42, 12, {"3": {"rubric_assessment": assessment, "expected_attempt": 1}}
        )
    assert item(result)["outcome"] == (
        "verified" if value == "Requested" else "unknown"
    )
    assert item(result)["grade_verified"] == (value == "Requested")
    assert sum(c.args[0] == "put" for c in mock.await_args_list) == 1
