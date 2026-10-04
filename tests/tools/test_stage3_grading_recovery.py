"""Synthetic batch recovery evidence without live Canvas calls."""

import json
from unittest.mock import AsyncMock, patch

import pytest
from fastmcp import FastMCP

from canvas_mcp.core.write_outcome import RequestFailure, WriteOutcome
from canvas_mcp.tools.assignments import register_educator_assignment_tools


async def tool(name="bulk_grade_submissions"):
    mcp = FastMCP("grading-recovery")
    register_educator_assignment_tools(mcp)
    return next(t.fn for t in await mcp.list_tools() if t.name == name)


def recovery(text):
    return json.loads(text.split("Recovery data (JSON):\n", 1)[1])


def submission(user_id=3, score=8, **overrides):
    return {
        "assignment_id": 12,
        "user_id": user_id,
        "attempt": 1,
        "score": score,
        "grade": str(score),
        "grade_matches_current_submission": True,
        "submission_comments": [],
        **overrides,
    }


@pytest.fixture(autouse=True)
def mock_course():
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
    "failure",
    [
        RequestFailure("Timed out", WriteOutcome.MAY_HAVE_WRITTEN),
        TimeoutError("Sensitive unknown failure"),
    ],
)
async def test_timeout_after_write_inspects_and_verifies_without_retry(failure):
    mock = AsyncMock(side_effect=[submission(score=0), failure, submission()])
    with patch("canvas_mcp.tools.assignments.make_canvas_request", mock):
        result = await (await tool())(42, 12, {"3": {"grade": 8}})
    report = recovery(result)
    assert report["items"][0]["outcome"] == "verified"
    assert report["items"][0]["write_evidence"] == "uncertain"
    assert [call.args[0] for call in mock.await_args_list] == ["get", "put", "get"]
    assert "Sensitive" not in result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "readback",
    [
        None,
        {},
        RequestFailure("Forbidden", WriteOutcome.REJECTED, 403),
        submission(user_id=4),
        submission(assignment_id=99),
        submission(attempt=2),
        submission(score=0),
        submission(grade_matches_current_submission=False),
    ],
)
async def test_wrong_unavailable_or_changed_readback_is_unknown(readback):
    mock = AsyncMock(side_effect=[submission(score=0), submission(), readback])
    with patch("canvas_mcp.tools.assignments.make_canvas_request", mock):
        result = await (await tool())(42, 12, {"3": {"grade": 8}})
    assert recovery(result)["items"][0]["outcome"] == "unknown"
    assert result.startswith("Error:")
    assert "Graded:  0" in result
    assert len([call for call in mock.await_args_list if call.args[0] == "put"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["before", "write", "after"])
@pytest.mark.parametrize("status", [401, 403])
async def test_revoked_access_stops_later_batches(phase, status):
    failure = RequestFailure("Do not disclose", WriteOutcome.REJECTED, status)
    responses = (
        [failure]
        if phase == "before"
        else [submission(), failure]
        if phase == "write"
        else [submission(), submission(), failure]
    )
    mock = AsyncMock(side_effect=responses)
    with patch("canvas_mcp.tools.assignments.make_canvas_request", mock):
        result = await (await tool())(
            42,
            12,
            {"3": {"grade": 8}, "4": {"grade": 9}},
            max_concurrent=1,
            rate_limit_delay=0,
        )
    items = recovery(result)["items"]
    assert items[1]["outcome"] == "unattempted"
    assert items[1]["reason"] == "stopped_after_authorization_failure"
    assert (
        items[0]["outcome"]
        == {"before": "unattempted", "write": "rejected", "after": "unknown"}[phase]
    )
    assert not any(call.args[1].endswith("/4") for call in mock.await_args_list)
    assert "Do not disclose" not in result


@pytest.mark.asyncio
async def test_partial_batch_reports_every_target_and_allows_safe_remaining_batch():
    calls = {}

    async def request(method, path, **kwargs):
        uid = int(path.rsplit("/", 1)[1])
        calls[uid] = calls.get(uid, 0) + 1
        if method == "put" and uid == 4:
            return RequestFailure("Invalid", WriteOutcome.REJECTED, 422)
        return submission(user_id=uid, score=8 if uid == 3 else 10 if uid == 5 else 0)

    mock = AsyncMock(side_effect=request)
    with patch("canvas_mcp.tools.assignments.make_canvas_request", mock):
        result = await (await tool())(
            42,
            12,
            {"3": {"grade": 8}, "4": {"grade": 9}, "5": {"grade": 10}},
            max_concurrent=2,
            rate_limit_delay=0,
        )
    report = recovery(result)
    assert [item["outcome"] for item in report["items"]] == [
        "verified",
        "rejected",
        "verified",
    ]
    assert report["counts"] == {
        "verified": 2,
        "rejected": 1,
        "unknown": 0,
        "unattempted": 0,
    }
    assert len([call for call in mock.await_args_list if call.args[0] == "put"]) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "comment_case", ["new", "existing", "wrong_author", "unavailable"]
)
async def test_comment_recovery_requires_new_persisted_id_and_authenticated_author(
    comment_case,
):
    before_comment = {"id": 1, "author_id": 99, "comment": "Feedback"}
    new_comment = {
        "id": 2,
        "author_id": 99 if comment_case != "wrong_author" else 77,
        "comment": "Feedback",
    }
    comments = (
        None
        if comment_case == "unavailable"
        else [before_comment]
        if comment_case == "existing"
        else [before_comment, new_comment]
    )
    mock = AsyncMock(
        side_effect=[
            {"id": 99},
            submission(submission_comments=[before_comment]),
            RequestFailure("Timeout", WriteOutcome.MAY_HAVE_WRITTEN),
            submission(submission_comments=comments),
        ]
    )
    with patch("canvas_mcp.tools.assignments.make_canvas_request", mock):
        result = await (await tool())(
            42, 12, {"3": {"grade": 8, "comment": "Feedback"}}
        )
    item = recovery(result)["items"][0]
    assert item["grade_verified"]
    assert item["outcome"] == ("verified" if comment_case == "new" else "unknown")
    assert "Feedback" not in result
    assert len([call for call in mock.await_args_list if call.args[0] == "put"]) == 1


@pytest.mark.asyncio
async def test_rubric_persistence_requires_criterion_values_and_total():
    assignment = {
        "id": 12,
        "use_rubric_for_grading": True,
        "rubric": [{"id": "a"}, {"id": "b", "ignore_for_scoring": True}],
    }
    assessment = {"a": {"points": 5}, "b": {"points": 9}}
    persisted = submission(
        score=5, rubric_assessment={"a": {"points": 5}, "b": {"points": 0}}
    )
    mock = AsyncMock(
        side_effect=[assignment, submission(), submission(score=5), persisted]
    )
    with patch("canvas_mcp.tools.assignments.make_canvas_request", mock):
        result = await (await tool())(42, 12, {"3": {"rubric_assessment": assessment}})
    assert recovery(result)["items"][0]["outcome"] == "unknown"
    assert "rubric_grade_unconfirmed" in result


@pytest.mark.asyncio
async def test_dry_run_has_unattempted_items_and_no_grade_requests():
    with patch(
        "canvas_mcp.tools.assignments.make_canvas_request", AsyncMock()
    ) as request:
        result = await (await tool())(
            42, 12, {"3": {"grade": 8}, "4": {"grade": 9}}, dry_run=True
        )
    assert all(
        item["outcome"] == "unattempted" and item["reason"] == "dry_run"
        for item in recovery(result)["items"]
    )
    request.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "grade,score", [("80%", 16), ("complete", 20), ("fail", 0), ("A", 19)]
)
async def test_documented_grade_formats_verified_in_persisted_state(grade, score):
    after = submission(score=score, grade="A" if grade == "A" else str(score))
    responses = ([{"points_possible": 20}] if grade != "A" else []) + [
        submission(score=0),
        after,
        after,
    ]
    with patch(
        "canvas_mcp.tools.assignments.make_canvas_request",
        AsyncMock(side_effect=responses),
    ):
        result = await (await tool())(42, 12, {"3": {"grade": grade}})
    assert recovery(result)["items"][0]["outcome"] == "verified"


@pytest.mark.asyncio
async def test_authorization_failure_preserves_dispatched_batch_but_stops_next():
    async def request(method, path, **kwargs):
        uid = int(path.rsplit("/", 1)[1])
        if method == "put" and uid == 3:
            return RequestFailure("Forbidden", WriteOutcome.REJECTED, 403)
        return submission(user_id=uid, score=9)

    mock = AsyncMock(side_effect=request)
    with patch("canvas_mcp.tools.assignments.make_canvas_request", mock):
        result = await (await tool())(
            42,
            12,
            {"3": {"grade": 8}, "4": {"grade": 9}, "5": {"grade": 10}},
            max_concurrent=2,
            rate_limit_delay=0,
        )
    items = recovery(result)["items"]
    assert [item["outcome"] for item in items] == [
        "rejected",
        "verified",
        "unattempted",
    ]
    assert any(
        call.args == ("put", "/courses/42/assignments/12/submissions/4")
        for call in mock.await_args_list
    )
    assert not any(call.args[1].endswith("/5") for call in mock.await_args_list)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "before",
    [
        submission(user_id=4),
        submission(attempt=None),
        submission(submission_comments=None),
    ],
)
async def test_unavailable_prewrite_target_or_comment_history_is_unattempted(before):
    mock = AsyncMock(side_effect=[{"id": 99}, before])
    with patch("canvas_mcp.tools.assignments.make_canvas_request", mock):
        result = await (await tool())(
            42, 12, {"3": {"grade": 8, "comment": "Feedback"}}
        )
    assert recovery(result)["items"][0]["outcome"] == "unattempted"
    assert not any(call.args[0] == "put" for call in mock.await_args_list)


@pytest.mark.asyncio
async def test_existing_matching_grade_is_observed_without_causality_claim():
    mock = AsyncMock(
        side_effect=[
            submission(),
            RequestFailure("Unknown", WriteOutcome.MAY_HAVE_WRITTEN),
            submission(),
        ]
    )
    with patch("canvas_mcp.tools.assignments.make_canvas_request", mock):
        result = await (await tool())(42, 12, {"3": {"grade": 8}})
    item = recovery(result)["items"][0]
    assert item["outcome"] == "verified"
    assert item["reason"] == "persisted_state_verified"
    assert item["write_evidence"] == "uncertain"
    assert item["recovery_action"] == "do_not_repeat"
