"""Synthetic regressions for peer-review and submission-status stabilization."""

from unittest.mock import AsyncMock

import pytest
from fastmcp import FastMCP

from canvas_mcp.tools import assignments, student_tools


async def tool(module, register, name):
    """Resolve the real decorated function from an isolated tool registry."""
    mcp = FastMCP("peer-status-regression")
    getattr(module, register)(mcp)
    return (await mcp.get_tool(name)).fn


@pytest.fixture
def educator(monkeypatch):
    monkeypatch.setattr(assignments, "get_course_id", AsyncMock(return_value="42"))
    monkeypatch.setattr(assignments, "get_course_code", AsyncMock(return_value="TEST"))
    request = AsyncMock()
    monkeypatch.setattr(assignments, "make_canvas_request", request)
    return request


@pytest.mark.asyncio
async def test_assign_peer_review_direct_lookup(educator):
    educator.side_effect = [{"id": 301, "user_id": 200}, {"id": 7}]
    fn = await tool(assignments, "register_educator_assignment_tools", "assign_peer_review")
    result = await fn("42", "9", "100", "200")
    assert "Successfully assigned" in result
    assert educator.await_args_list[0].args == (
        "get", "/courses/42/assignments/9/submissions/200"
    )
    assert educator.await_args_list[1].args == (
        "post", "/courses/42/assignments/9/submissions/301/peer_reviews"
    )
    assert educator.await_args_list[1].kwargs == {"data": {"user_id": "100"}}


@pytest.mark.asyncio
@pytest.mark.parametrize("record", [
    [], {"error": "not found"}, {}, {"id": 301, "user_id": 201},
    {"id": "301/other", "user_id": 200},
])
async def test_assign_peer_review_refuses_unusable_record(educator, record):
    educator.return_value = record
    fn = await tool(assignments, "register_educator_assignment_tools", "assign_peer_review")
    result = await fn("42", "9", "100", "200")
    assert "No peer review was assigned and no submission was created" in result
    assert educator.await_count == 1
    assert educator.await_args.args[0] == "get"


@pytest.mark.asyncio
@pytest.mark.parametrize("reviewee", ["self", "200/other", "２００"])
async def test_assign_peer_review_refuses_nonnumeric_id(educator, reviewee):
    fn = await tool(assignments, "register_educator_assignment_tools", "assign_peer_review")
    result = await fn("42", "9", "100", reviewee)
    assert "numeric Canvas user ID" in result
    educator.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("available_reviews", [[], [
    {"assessor_id": 100, "user_id": 200, "workflow_state": "assigned"},
    {"assessor_id": 101, "user_id": 200, "workflow_state": "completed"},
    {"user_id": 200, "workflow_state": "assigned"},
]])
async def test_list_reviews_paginates_and_discloses_failures(
    monkeypatch, educator, available_reviews
):
    fetch = AsyncMock(side_effect=[
        [{"id": 301, "user_id": 200}, {"id": 302, "user_id": 201}],
        [{"id": 100, "name": "Reviewer A"}, {"id": 101, "name": "Reviewer B"},
         {"id": 200, "name": "Reviewee"}],
        available_reviews, {"error": "read failed"},
    ])
    monkeypatch.setattr(assignments, "fetch_all_paginated_results", fetch)
    fn = await tool(assignments, "register_educator_assignment_tools", "list_peer_reviews")
    result = await fn("42", "9")
    assert "this list is incomplete" in result
    assert "User 201:" in result and "read failed" in result
    assert "UNTRUSTED CANVAS CONTENT (Canvas error diagnostic)" in result
    assert "No peer reviews found for this assignment" not in result
    for call in fetch.await_args_list[2:]:
        assert call.args[0].endswith("/peer_reviews")
        assert call.args[1] == {"per_page": 100}
    educator.assert_not_awaited()
    if available_reviews:
        assert "Reviewer A" in result and "Reviewer B" in result
        assert "(ID: unknown)" in result
        assert "Reviewer: " in result
        reviewer_lines = [line for line in result.splitlines() if "Reviewer:" in line]
        assert all("Reviewee" not in line for line in reviewer_lines)
    else:
        assert "submissions that could be read" in result


@pytest.mark.asyncio
@pytest.mark.parametrize("due_at", ["2000-01-01T00:00:00Z", "2099-01-01T00:00:00Z", None])
@pytest.mark.parametrize(("submission", "types", "expected"), [
    ({"missing": True, "workflow_state": "unsubmitted"}, ["online_text_entry"], "OVERDUE"),
    ({"missing": False, "workflow_state": "unsubmitted"}, ["online_text_entry"], "NOT SUBMITTED"),
    ({"workflow_state": "graded"}, ["online_text_entry"], "GRADED"),
    ({"excused": True, "missing": True}, ["online_text_entry"], "EXCUSED"),
    ({"workflow_state": "unsubmitted"}, ["on_paper"], "NO ONLINE SUBMISSION REQUIRED"),
    ({"workflow_state": "unsubmitted"}, ["none"], "NO ONLINE SUBMISSION REQUIRED"),
    ({"missing": True}, ["external_tool"], "Canvas does not report external-tool submission state"),
])
async def test_student_status_uses_canvas_state(
    monkeypatch, submission, types, expected, due_at
):
    monkeypatch.setattr(student_tools, "get_course_id", AsyncMock(return_value="42"))
    monkeypatch.setattr(student_tools, "get_course_code", AsyncMock(return_value="TEST"))
    fetch = AsyncMock(return_value=[{
        "id": 9, "name": "Assignment", "due_at": due_at,
        "submission_types": types, "submission": submission,
    }])
    monkeypatch.setattr(student_tools, "fetch_all_paginated_results", fetch)
    fn = await tool(student_tools, "register_student_tools", "get_my_submission_status")
    result = await fn("42")
    assert expected in result
    if expected != "OVERDUE":
        assert "OVERDUE" not in result
    if expected not in {"OVERDUE", "NOT SUBMITTED"}:
        assert "NOT SUBMITTED" not in result
        assert "Awaiting Submission" not in result
