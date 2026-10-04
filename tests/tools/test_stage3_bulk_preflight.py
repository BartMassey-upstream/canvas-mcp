"""Offline whole-batch preflight checks prevent partial invalid grading."""

from unittest.mock import AsyncMock, patch

import pytest
from fastmcp import FastMCP

from canvas_mcp.tools.assignments import register_educator_assignment_tools


@pytest.fixture(autouse=True)
def mock_course_cache():
    with (
        patch("canvas_mcp.tools.assignments.get_course_id", AsyncMock(return_value=42)),
        patch(
            "canvas_mcp.tools.assignments.get_course_code",
            AsyncMock(return_value="Synthetic"),
        ),
    ):
        yield


async def grade_tool():
    mcp = FastMCP("stage3-bulk-preflight")
    register_educator_assignment_tools(mcp)
    return next(
        t.fn for t in await mcp.list_tools() if t.name == "bulk_grade_submissions"
    )


INVALID = [
    None,
    [],
    {},
    {"comment": "Comment only"},
    {"grade": None},
    {"grade": True},
    {"grade": float("nan")},
    {"grade": "Infinity"},
    {"grade": 1, "typo": 2},
    {"grade": 1, "comment": []},
    {"rubric_assessment": {}},
    {"rubric_assessment": []},
    {"rubric_assessment": {"_1": {"points": float("inf")}}},
    {"rubric_assessment": {"_1": {"points": "2"}}},
    {"rubric_assessment": {"_1": {"points": -1}}},
    {"rubric_assessment": {"_1": {"points": True}}},
    {"rubric_assessment": {"_1": {"comments": "Missing points"}}},
    {"rubric_assessment": {"_1": {"points": 1, "unknown": 2}}},
    {"rubric_assessment": {"_1][other": {"points": 1}}},
    {"rubric_assessment": {"_1": {"points": 1, "rating_id": []}}},
    {"rubric_assessment": {"_1": {"points": 1, "comments": {}}}},
    {"grade": "<<<UNTRUSTED CANVAS CONTENT (test): x>>>"},
    {"grade": 1, "comment": "<<<UNTRUSTED CANVAS CONTENT (test): x>>>"},
    {
        "rubric_assessment": {
            "_1": {"points": 1, "comments": "<<<UNTRUSTED CANVAS CONTENT (test): x>>>"}
        }
    },
]


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", INVALID)
@pytest.mark.parametrize("dry_run", [False, True])
async def test_invalid_later_grade_prevents_entire_batch(invalid, dry_run):
    with (
        patch(
            "canvas_mcp.tools.assignments.make_canvas_request", AsyncMock()
        ) as request,
        patch("canvas_mcp.tools.assignments.get_course_id", AsyncMock()) as course,
    ):
        result = await (await grade_tool())(
            42, 12, {"3": {"grade": 8}, "4": invalid}, dry_run=dry_run, max_concurrent=1
        )
    assert "No grades were submitted" in result
    request.assert_not_awaited()
    course.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "user_id", ["self", "-1", "0", "3/submissions/4", " 3", "３", ""]
)
async def test_invalid_later_target_prevents_all_requests(user_id):
    with patch(
        "canvas_mcp.tools.assignments.make_canvas_request", AsyncMock()
    ) as request:
        result = await (await grade_tool())(
            42, 12, {"3": {"grade": 8}, user_id: {"grade": 9}}
        )
    assert "positive Canvas user ID" in result
    request.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "options",
    [
        {"max_concurrent": 0},
        {"max_concurrent": -1},
        {"max_concurrent": 21},
        {"rate_limit_delay": -1},
        {"rate_limit_delay": float("nan")},
        {"rate_limit_delay": float("inf")},
    ],
)
async def test_invalid_batch_controls_prevent_requests(options):
    with patch(
        "canvas_mcp.tools.assignments.make_canvas_request", AsyncMock()
    ) as request:
        result = await (await grade_tool())(42, 12, {"3": {"grade": 8}}, **options)
    assert "Error:" in result
    request.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("grade", [8, 0, "A", "80%", "complete"])
async def test_valid_grade_formats_keep_dry_run_without_writes(grade):
    with (
        patch(
            "canvas_mcp.tools.assignments.make_canvas_request", AsyncMock()
        ) as request,
        patch("canvas_mcp.tools.assignments.get_course_id", AsyncMock(return_value=42)),
        patch(
            "canvas_mcp.tools.assignments.get_course_code",
            AsyncMock(return_value="Synthetic"),
        ),
    ):
        result = await (await grade_tool())(
            42,
            12,
            {"3": {"grade": grade}},
            dry_run=True,
            max_concurrent=20,
            rate_limit_delay=0,
        )
    assert "DRY RUN" in result
    assert not any(call.args[0] != "get" for call in request.await_args_list)


@pytest.mark.parametrize("options", [(True, 0), (1.5, 0), (1, True)])
def test_preflight_rejects_raw_malformed_control_types(options):
    from canvas_mcp.tools.assignments import _bulk_grading_preflight

    assert "Error:" in _bulk_grading_preflight({"3": {"grade": 8}}, *options)


@pytest.mark.asyncio
@pytest.mark.parametrize("grade", ["NaN%", "Infinity%", "-Infinity%"])
async def test_nonfinite_percentage_grade_prevents_all_requests(grade):
    with patch(
        "canvas_mcp.tools.assignments.make_canvas_request", AsyncMock()
    ) as request:
        result = await (await grade_tool())(42, 12, {"3": {"grade": grade}})
    assert "Numeric grades must be finite" in result
    request.assert_not_awaited()


@pytest.mark.asyncio
async def test_duplicate_numeric_target_aliases_prevent_all_requests():
    with patch("canvas_mcp.tools.assignments.make_canvas_request", AsyncMock()) as request:
        result = await (await grade_tool())(42, 12, {"3": {"grade": 8}, "03": {"grade": 9}})
    assert "Duplicate Canvas user IDs" in result
    request.assert_not_awaited()
