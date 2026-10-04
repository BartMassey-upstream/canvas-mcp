"""Weekly review distinguishes unavailable observations from missing work."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastmcp import FastMCP

from canvas_mcp.tools.assignments import register_educator_assignment_tools


async def analytics(assignment, students, submissions):
    mcp = FastMCP("analytics-observations")
    register_educator_assignment_tools(mcp)
    tool = next(
        t.fn for t in await mcp.list_tools() if t.name == "get_assignment_analytics"
    )
    with (
        patch("canvas_mcp.tools.assignments.get_course_id", AsyncMock(return_value=42)),
        patch(
            "canvas_mcp.tools.assignments.get_course_code",
            AsyncMock(return_value="Synthetic"),
        ),
        patch(
            "canvas_mcp.tools.assignments.get_config",
            return_value=SimpleNamespace(
                canvas_api_url="https://canvas.invalid/api/v1"
            ),
        ),
        patch(
            "canvas_mcp.tools.assignments.make_canvas_request",
            AsyncMock(return_value=assignment),
        ) as request,
        patch(
            "canvas_mcp.tools.assignments.fetch_all_paginated_results",
            AsyncMock(side_effect=[students, submissions]),
        ),
    ):
        result = await tool(42, 12)
    assert all(call.args[0] == "get" for call in request.await_args_list)
    return result


@pytest.mark.asyncio
async def test_observation_sources_denominator_and_absent_records_are_explicit():
    result = await analytics(
        {
            "id": 12,
            "name": "Synthetic",
            "points_possible": 20,
            "published": True,
            "due_at": None,
        },
        [{"id": 3, "name": "Alpha"}, {"id": 4, "name": "Beta"}],
        [
            {
                "user_id": 3,
                "submitted_at": None,
                "score": None,
                "missing": False,
                "late": False,
                "excused": False,
                "workflow_state": "unsubmitted",
            }
        ],
    )
    assert "Observation started:" in result
    assert "Observation completed:" in result
    assert "https://canvas.invalid/courses/42/assignments/12" in result
    assert "https://canvas.invalid/api/v1/courses/42/users" in result
    assert (
        "https://canvas.invalid/api/v1/courses/42/assignments/12/submissions" in result
    )
    assert "Missing: 0/2" in result
    assert "No returned submission record: 1/2 (unknown" in result
    assert "submitted_at: 1/2" in result
    assert "score: 1/2" in result
    assert "Students Missing Submission:" not in result


@pytest.mark.asyncio
async def test_missing_flags_and_malformed_fields_count_as_unavailable():
    result = await analytics(
        {"id": 12, "name": "Synthetic", "points_possible": 0},
        [{"id": 3}, {"id": 4}],
        [
            {"user_id": 3},
            {
                "user_id": 4,
                "submitted_at": "bad",
                "score": "bad",
                "missing": "false",
                "late": None,
                "excused": 0,
                "workflow_state": [],
            },
        ],
    )
    assert "Missing: 0/2" in result
    for field in (
        "score",
        "late",
        "missing",
        "excused",
        "workflow_state",
        "submitted_at",
    ):
        assert f"{field}: 2/2" in result
    assert "Assignment fields absent: 2/3" in result
    assert "Grade Statistics:" not in result


@pytest.mark.asyncio
async def test_report_only_counts_visible_roster_and_explicit_missing_flags():
    result = await analytics(
        {"id": 12, "points_possible": 20},
        [{"id": 3, "name": "Observed"}],
        [
            {
                "user_id": 3,
                "submitted_at": None,
                "score": None,
                "missing": True,
                "late": False,
                "excused": False,
                "workflow_state": "unsubmitted",
            },
            {"user_id": 99, "missing": True},
        ],
    )
    assert "Missing: 1/1" in result
    assert "Submission records outside visible roster: 1 (excluded)" in result
    assert "No returned submission record: 0/1" in result


@pytest.mark.asyncio
@pytest.mark.parametrize("family", ["assignment", "roster", "submissions"])
async def test_unavailable_source_is_error_with_observation_not_empty(family):
    assignment = (
        {"error": "Private remote details"} if family == "assignment" else {"id": 12}
    )
    roster = {"error": "Private remote details"} if family == "roster" else [{"id": 3}]
    submissions = {"error": "Private remote details"} if family == "submissions" else []
    result = await analytics(assignment, roster, submissions)
    assert result.startswith("Error:")
    assert "Unavailable:" in result
    assert "Observation started:" in result
    assert "Private remote" not in result
    assert "No students found" not in result


@pytest.mark.asyncio
async def test_zero_points_with_visible_score_reports_without_dividing_by_zero():
    result = await analytics(
        {"id": 12, "points_possible": 0}, [{"id": 3}], [{"user_id": 3, "score": 0}]
    )
    assert "Median Score: 0/0 (unavailable%)" in result


@pytest.mark.asyncio
async def test_empty_visible_roster_remains_scoped_observation():
    result = await analytics({"id": 12}, [], [])
    assert "Submitted: 0/0" in result
    assert "visible student roster" in result
    assert "No returned submission record: 0/0" in result
