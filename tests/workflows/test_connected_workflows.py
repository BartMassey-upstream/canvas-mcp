"""Connected MCP read and draft workflows against reusable synthetic Canvas."""

import json
import re

import pytest


def result_text(result):
    assert not result.is_error
    return "\n".join(block.text for block in result.content if hasattr(block, "text"))


def result_data(result):
    return json.loads(result_text(result))


@pytest.mark.asyncio
async def test_educator_reviews_paginated_work_then_drafts_followup(workflow_session):
    async with workflow_session() as (mcp, canvas):
        identity = result_text(await mcp.call_tool("get_my_enrollments", {}))
        assert "TeacherEnrollment" in identity
        analytics = result_text(
            await mcp.call_tool(
                "get_assignment_analytics",
                {
                    "course_identifier": "SYNTH/42",
                    "assignment_id": 34,
                },
            )
        )
        assert "Submitted: 2/4" in analytics
        assert "Graded: 1/4" in analytics
        assert "Missing: 1/4" in analytics
        assert "Late: 1/2" in analytics
        assert "Excused: 1" in analytics
        assert "UNTRUSTED CANVAS CONTENT" in analytics
        submissions = result_text(
            await mcp.call_tool(
                "list_submissions",
                {
                    "course_identifier": 42,
                    "assignment_id": 34,
                },
            )
        )
        assert all(f"User ID: {n}" in submissions for n in range(101, 105))
        draft = result_data(
            await mcp.call_tool(
                "send_conversation",
                {
                    "course_identifier": 42,
                    "recipient_ids": ["101"],
                    "subject": "Practice followup",
                    "body": "Please review your practice status.",
                },
            )
        )
        assert draft["preview"] and draft["nothing_sent"]
        assert draft["recipient_ids"] == ["101"]
        assert draft["confirmation_token"]
        paths = [path for _, path in canvas.requests]
        assert paths.count("/courses/42/users") == 2
        assert paths.count("/courses/42/assignments/34/submissions") == 4
        assert not canvas.writes


@pytest.mark.asyncio
async def test_restricted_educator_reports_unavailable_roster_not_empty(
    workflow_session,
):
    async with workflow_session(actor="restricted") as (mcp, canvas):
        identity = result_text(await mcp.call_tool("get_my_enrollments", {}))
        assert "TeacherEnrollment" in identity
        response = await mcp.call_tool(
            "get_assignment_analytics",
            {
                "course_identifier": 42,
                "assignment_id": 34,
            },
            raise_on_error=False,
        )
        assert response.is_error
        result = response.content[0].text
        assert "fetching students" in result
        assert "No students found" not in result
        assert ("GET", "/courses/42/assignments/34/submissions") not in canvas.requests
        assert not canvas.writes


@pytest.mark.asyncio
async def test_group_review_preserves_partial_failure_and_missing_fields(
    workflow_session,
):
    async with workflow_session() as (mcp, canvas):
        canvas.failures["/groups/62/users"] = 403
        groups = result_text(
            await mcp.call_tool("list_groups", {"course_identifier": "SYNTH/42"})
        )
        assert "Group A" in groups and "Group B" in groups
        assert "Synthetic learner 101" in groups and "Synthetic learner 102" in groups
        assert "No email" in groups
        assert "Error fetching members" in groups
        assert "UNTRUSTED CANVAS CONTENT" in groups
        assert not canvas.writes


@pytest.mark.asyncio
async def test_student_planning_reads_self_and_external_state_without_writes(
    workflow_session,
):
    async with workflow_session(actor="student", profile="student") as (mcp, canvas):
        names = {tool.name for tool in await mcp.list_tools()}
        assert (
            not {
                "submit_assignment",
                "create_planner_note",
                "send_conversation",
                "execute_typescript",
            }
            & names
        )
        profile = result_text(await mcp.call_tool("get_my_profile", {}))
        assert "User ID: 101" in profile
        enrollments = result_text(await mcp.call_tool("get_my_enrollments", {}))
        assert "StudentEnrollment" in enrollments
        status = result_text(
            await mcp.call_tool("get_my_submission_status", {"course_identifier": 42})
        )
        assert "Missing Submissions (1)" in status
        assert "OVERDUE" in status
        assert "External-tool assignments (1)" in status
        assert "Canvas does not report external-tool submission state" in status
        assert "UNTRUSTED CANVAS CONTENT" in status
        assert not any(
            path.endswith("/users") or path.endswith("/submissions")
            for _, path in canvas.requests
        )
        assert not canvas.writes


@pytest.mark.asyncio
async def test_stale_page_preview_rejects_changed_target_without_write(
    workflow_session,
):
    async with workflow_session(profile="creator") as (mcp, canvas):
        args = {"course_identifier": 42, "page_url_or_id": "overview"}
        preview = result_text(await mcp.call_tool("delete_page", args))
        token = re.search(r"Confirmation token: ([A-Za-z0-9_.-]+)", preview)
        assert token, preview
        canvas.change("/courses/42/pages/overview", title="Changed overview")
        response = await mcp.call_tool(
            "delete_page",
            {
                **args,
                "confirmation_token": token.group(1),
            },
            raise_on_error=False,
        )
        assert response.is_error
        rejected = response.content[0].text
        assert "changed" in rejected.lower() or "match" in rejected.lower()
        assert (
            canvas.objects["/courses/42/pages/overview"]["title"] == "Changed overview"
        )
        assert not canvas.writes


@pytest.mark.asyncio
async def test_operator_read_only_policy_removes_draft_and_write_tools(
    workflow_session,
):
    async with workflow_session(allowed_write_tools="") as (mcp, canvas):
        names = {tool.name for tool in await mcp.list_tools()}
        assert {
            "get_my_enrollments",
            "get_assignment_analytics",
            "get_course_structure",
        } <= names
        assert (
            not {
                "send_conversation",
                "delete_page",
                "create_assignment",
                "execute_typescript",
            }
            & names
        )
        result_text(await mcp.call_tool("get_my_enrollments", {}))
        assert not canvas.writes
