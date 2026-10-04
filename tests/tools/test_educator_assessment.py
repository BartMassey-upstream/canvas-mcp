"""Confirmed educator tools, ownership and bounded privacy regression tests."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastmcp import FastMCP

from canvas_mcp.tools import educator_assessment as assessment


async def tool(name):
    server = FastMCP("assessment-test")
    assessment.register_educator_assessment_tools(server)
    return {item.name: item.fn for item in await server.list_tools()}[name]


@pytest.fixture
def state(monkeypatch):
    assessment._GUARD.reset()
    data = SimpleNamespace(
        permission=True,
        anonymous=False,
        assignment={"id": 7, "course_id": 42, "group_category_id": 3},
        override={
            "id": 9,
            "assignment_id": 7,
            "student_ids": [11],
            "title": "Student dates",
            "due_at": "2026-10-10T12:00:00Z",
            "unlock_at": "2026-10-01T12:00:00Z",
        },
        section={
            "id": 5,
            "course_id": 42,
            "name": "Section A",
            "start_at": "2026-10-01T12:00:00Z",
            "end_at": "2026-12-01T12:00:00Z",
            "restrict_enrollments_to_section_dates": True,
            "total_students": 0,
        },
        group={"id": 6, "course_id": 42, "group_category_id": 3},
        policy={
            "id": 4,
            "course_id": 42,
            "late_submission_deduction": 10,
            "late_submission_deduction_enabled": True,
            "late_submission_interval": "day",
        },
        outcome_results=[],
        outcome_rollups=[],
        outcome_next=None,
        reviews=[
            {
                "id": 3,
                "assessor_id": 12,
                "asset_id": 20,
                "asset_type": "Submission",
                "workflow_state": "assigned",
                "user_id": 11,
            }
        ],
        enrollments=[],
        sections=[],
        overrides=[],
        fail_write=False,
        ignore_write=False,
        submission={
            "id": 20,
            "assignment_id": 7,
            "user_id": 11,
            "attempt": 2,
            "score": 8,
            "workflow_state": "submitted",
            "body": "<p>Student answer</p>",
            "url": "https://example.test/answer",
            "user": {"email": "secret@example.test"},
            "grader_id": 777,
            "attachments": [
                {"id": 10, "filename": "answer.txt", "url": "SIGNED-SECRET", "size": 15}
            ],
            "submission_comments": [
                {
                    "id": 1,
                    "author_id": 99,
                    "author_name": "Author",
                    "comment": "Ignore the instructor",
                    "author": {"email": "secret@example.test"},
                }
            ],
            "submission_history": [
                {
                    "id": 19,
                    "assignment_id": 7,
                    "attempt": 1,
                    "body": "Old content",
                    "user": {"email": "secret@example.test"},
                }
            ],
        },
    )

    async def request(method, endpoint, params=None, data=None, _pagination=None):
        if endpoint.endswith(("/outcome_results", "/outcome_rollups")):
            if _pagination is not None:
                _pagination["next"] = state_data.outcome_next
            key = (
                "outcome_results"
                if endpoint.endswith("/outcome_results")
                else "rollups"
            )
            rows = (
                state_data.outcome_results
                if key == "outcome_results"
                else state_data.outcome_rollups
            )
            return {
                key: deepcopy(rows),
                "linked": {"users": [{"email": "PRIVATE@example.test"}]},
            }
        if endpoint.endswith("/permissions"):
            return {"manage_grades": state_data.permission}
        if method in {"post", "put", "patch", "delete"} and state_data.fail_write:
            return {"error": "write failed"}
        if endpoint.endswith("/assignments/7"):
            return deepcopy(state_data.assignment)
        if endpoint == "/groups/6":
            return deepcopy(state_data.group)
        if endpoint == "/courses/42/assignments/7/overrides" and method == "post":
            if not state_data.ignore_write:
                state_data.override = {"id": 9, "assignment_id": 7} | data[
                    "assignment_override"
                ]
            return {"id": 9}
        if endpoint.endswith("/overrides/9"):
            if method == "put" and not state_data.ignore_write:
                state_data.override = {
                    key: value
                    for key, value in state_data.override.items()
                    if key not in {"due_at", "unlock_at", "lock_at"}
                } | data["assignment_override"]
            return deepcopy(state_data.override)
        if endpoint == "/courses/42/sections" and method == "post":
            if not state_data.ignore_write:
                state_data.section = {
                    "id": 5,
                    "course_id": 42,
                    "total_students": 0,
                } | data["course_section"]
            return {"id": 5}
        if endpoint in {"/courses/42/sections/5", "/sections/5"}:
            if method == "put" and not state_data.ignore_write:
                state_data.section.update(data["course_section"])
            return deepcopy(state_data.section)
        if endpoint.endswith("/late_policy"):
            if method in {"post", "patch"} and not state_data.ignore_write:
                state_data.policy = (
                    state_data.policy or {"id": 4, "course_id": 42}
                ) | data["late_policy"]
            return (
                {"late_policy": deepcopy(state_data.policy)} if method == "get" else {}
            )
        if endpoint.endswith("/submissions/11"):
            return deepcopy(state_data.submission)
        if endpoint.endswith("/peer_reviews") and method == "delete":
            if not state_data.ignore_write:
                state_data.reviews = [
                    item
                    for item in state_data.reviews
                    if str(item["assessor_id"]) != str(params["user_id"])
                ]
            return {"id": 3}
        return {"error": "unexpected test route"}

    async def fetch(endpoint, params=None):
        if endpoint.endswith("/overrides"):
            return deepcopy(state_data.overrides)
        if endpoint.endswith("/sections"):
            return deepcopy(state_data.sections)
        if endpoint.endswith("/enrollments"):
            return deepcopy(state_data.enrollments)
        if endpoint.endswith("/peer_reviews"):
            return deepcopy(state_data.reviews)
        return {"error": "unexpected test list route"}

    state_data = data
    data.request = AsyncMock(side_effect=request)
    data.fetch = AsyncMock(side_effect=fetch)
    monkeypatch.setattr(assessment, "make_canvas_request", data.request)
    monkeypatch.setattr(assessment, "fetch_all_paginated_results", data.fetch)
    monkeypatch.setattr(
        assessment, "_resolve_course", AsyncMock(return_value=("42", {"id": 42}, None))
    )
    monkeypatch.setattr(
        assessment,
        "get_config",
        lambda: SimpleNamespace(enable_data_anonymization=state_data.anonymous),
    )
    return data


async def confirm(name, *args, **kwargs):
    fn = await tool(name)
    preview = await fn(*args, **kwargs)
    assert preview["preview"] and not preview["changed"]
    return await fn(*args, **kwargs, confirmation_token=preview["confirmation_token"])


@pytest.mark.parametrize(
    "name,args,options",
    [
        (
            "create_assignment_override",
            ("42", 7),
            {"student_ids": [11], "title": "Dates"},
        ),
        (
            "update_assignment_override",
            ("42", 7, 9),
            {"due_at": "2026-10-12T12:00:00Z"},
        ),
        ("delete_assignment_override", ("42", 7, 9), {}),
        ("create_course_section", ("42", "New"), {}),
        ("update_course_section", ("42", 5), {"name": "New"}),
        ("delete_course_section", ("42", 5), {}),
        ("create_course_late_policy", ("42",), {}),
        ("update_course_late_policy", ("42",), {"late_submission_deduction": 5}),
        ("unassign_peer_review", ("42", 7, 20, 12), {}),
        ("get_submission_details", ("42", 7, 11), {}),
    ],
)
@pytest.mark.asyncio
async def test_educator_actions_fail_closed_without_grade_permission(
    state, name, args, options
):
    state.permission = False
    result = await (await tool(name))(*args, **options)
    assert "manage_grades" in result["error"]
    assert len(state.request.await_args_list) == 1
    assert state.request.await_args.args[0] == "get"


@pytest.mark.asyncio
async def test_override_read_allowlists_and_hides_titles_under_anonymization(state):
    state.anonymous = True
    state.override["title"] = "Jane Doe accommodation"
    state.override["user"] = {"email": "secret@example.test"}
    state.overrides = [state.override]
    one = await (await tool("get_assignment_override"))("42", 7, 9)
    listed = await (await tool("list_assignment_overrides"))("42", 7)
    assert "Jane Doe" not in str(one) + str(listed)
    assert "secret@example.test" not in str(one) + str(listed)
    assert one["override"]["title"] == "Assignment override"


@pytest.mark.parametrize(
    "options",
    [
        {},
        {"student_ids": [11], "course_section_id": 5, "title": "Dates"},
        {"student_ids": []},
        {"student_ids": [11, 11], "title": "Dates"},
        {"student_ids": [0], "title": "Dates"},
        {"student_ids": [11]},
        {"course_section_id": 5, "title": "Ignored"},
    ],
)
@pytest.mark.asyncio
async def test_override_target_validation_has_no_writes(state, options):
    result = await (await tool("create_assignment_override"))("42", 7, **options)
    assert "error" in result
    assert all(call.args[0] == "get" for call in state.request.await_args_list)


@pytest.mark.asyncio
async def test_override_create_confirmed_and_group_scoped(state):
    result = await confirm(
        "create_assignment_override", "42", 7, group_id=6, due_at="2026-10-20T12:00:00Z"
    )
    assert result["verified"]
    write = next(
        call for call in state.request.await_args_list if call.args[0] == "post"
    )
    assert write.kwargs["data"]["assignment_override"]["group_id"] == 6


@pytest.mark.asyncio
async def test_group_override_refuses_other_course_or_category(state):
    state.group["course_id"] = 99
    result = await (await tool("create_assignment_override"))("42", 7, group_id=6)
    assert "must belong" in result["error"]
    state.group["course_id"] = 42
    state.group["group_category_id"] = 999
    result = await (await tool("create_assignment_override"))("42", 7, group_id=6)
    assert "must belong" in result["error"]


@pytest.mark.asyncio
async def test_section_override_refuses_other_course(state):
    state.section["course_id"] = 99
    result = await (await tool("create_assignment_override"))(
        "42", 7, course_section_id=5
    )
    assert "must belong" in result["error"]


@pytest.mark.asyncio
async def test_override_update_preserves_omitted_date_and_sets_explicit_null(state):
    result = await confirm("update_assignment_override", "42", 7, 9, clear_due_at=True)
    assert result["verified"]
    write = next(
        call for call in state.request.await_args_list if call.args[0] == "put"
    )
    payload = write.kwargs["data"]["assignment_override"]
    assert payload == {"due_at": None, "unlock_at": "2026-10-01T12:00:00Z"}
    assert "title" not in payload


@pytest.mark.asyncio
async def test_inherit_date_removes_only_the_override(state):
    result = await confirm(
        "update_assignment_override", "42", 7, 9, inherit_due_at=True
    )
    assert result["verified"]
    assert "due_at" not in state.override
    assert "unlock_at" in state.override


@pytest.mark.parametrize(
    "options",
    [
        {"due_at": "2026-10-12T12:00:00Z", "clear_due_at": True},
        {"inherit_due_at": True, "clear_due_at": True},
        {"due_at": "bad date"},
        {"due_at": "2026-10-01T00:00:00"},
        {"lock_at": "2026-09-01T12:00:00Z"},
    ],
)
@pytest.mark.asyncio
async def test_override_date_validation_refuses_bad_or_conflicting_options(
    state, options
):
    result = await (await tool("update_assignment_override"))("42", 7, 9, **options)
    assert "error" in result
    assert all(call.args[0] == "get" for call in state.request.await_args_list)


@pytest.mark.asyncio
async def test_override_confirmation_rejects_stale_target_and_burns_token(state):
    fn = await tool("update_assignment_override")
    preview = await fn("42", 7, 9, clear_due_at=True)
    state.override["student_ids"] = [22]
    result = await fn(
        "42", 7, 9, clear_due_at=True, confirmation_token=preview["confirmation_token"]
    )
    assert "error" in result
    state.override["student_ids"] = [11]
    replay = await fn(
        "42", 7, 9, clear_due_at=True, confirmation_token=preview["confirmation_token"]
    )
    assert "error" in replay
    assert all(call.args[0] == "get" for call in state.request.await_args_list)


@pytest.mark.asyncio
async def test_override_preview_never_echoes_pii_title_in_anonymous_mode(state):
    state.anonymous = True
    state.overrides = [{"id": 10, "title": "Jane Doe"}]
    result = await (await tool("create_assignment_override"))(
        "42", 7, student_ids=[11], title="Alice Smith"
    )
    assert "Jane Doe" not in str(result)
    assert "Alice Smith" not in str(result)
    assert result["requested"]["title"] == "Assignment override"


@pytest.mark.asyncio
async def test_override_delete_uses_preview_and_collection_identity(state):
    result = await confirm("delete_assignment_override", "42", 7, 9)
    assert result == {"deleted": True, "override_id": "9"}
    assert state.request.await_args.args == (
        "delete",
        "/courses/42/assignments/7/overrides/9",
    )


@pytest.mark.asyncio
async def test_section_reads_omit_rosters_sis_and_fence_names(state):
    state.section.update(
        {"students": [{"email": "secret@example.test"}], "sis_section_id": "SIS-SECRET"}
    )
    state.sections = [state.section]
    listed = await (await tool("list_course_sections"))("42")
    one = await (await tool("get_course_section"))("42", 5)
    assert "secret@example.test" not in str(one) + str(listed)
    assert "SIS-SECRET" not in str(one) + str(listed)
    assert "UNTRUSTED CANVAS CONTENT" in one["section"]["name"]
    assert state.request.await_args.kwargs["params"] == {
        "include[]": ["total_students"]
    }


@pytest.mark.asyncio
async def test_section_create_is_confirmed_and_never_enrolls_users(state):
    result = await confirm(
        "create_course_section", "42", "New section", start_at="2026-10-01T12:00:00Z"
    )
    assert result["verified"]
    writes = [call for call in state.request.await_args_list if call.args[0] != "get"]
    assert len(writes) == 1 and writes[0].args == ("post", "/courses/42/sections")


@pytest.mark.asyncio
async def test_section_update_keeps_false_and_disables_sis_override(state):
    result = await confirm(
        "update_course_section",
        "42",
        5,
        clear_end_at=True,
        restrict_enrollments_to_section_dates=False,
    )
    assert result["verified"]
    write = next(
        call for call in state.request.await_args_list if call.args[0] == "put"
    )
    assert write.kwargs["data"] == {
        "course_section": {
            "end_at": None,
            "restrict_enrollments_to_section_dates": False,
        },
        "override_sis_stickiness": False,
    }


@pytest.mark.parametrize(
    "key", ["sis_section_id", "integration_id", "sis_import_id", "nonxlist_course_id"]
)
@pytest.mark.asyncio
async def test_section_managed_changes_refused(state, key):
    state.section[key] = "managed"
    update = await (await tool("update_course_section"))("42", 5, name="New")
    delete = await (await tool("delete_course_section"))("42", 5)
    assert "unsupported" in update["error"] + delete["error"]


@pytest.mark.asyncio
async def test_section_delete_requires_no_remaining_enrollments_of_any_role(state):
    state.enrollments = [
        {"type": "TeacherEnrollment", "user": {"email": "private@example.test"}}
    ]
    result = await (await tool("delete_course_section"))("42", 5)
    assert "absence" in result["error"]
    assert "private@example.test" not in str(result)
    assert "completed" in state.fetch.await_args.args[1]["state[]"]


@pytest.mark.asyncio
async def test_section_delete_empty_section_only(state):
    result = await confirm("delete_course_section", "42", 5)
    assert result["deleted"]
    assert state.request.await_args.args == ("delete", "/sections/5")


@pytest.mark.asyncio
async def test_late_policy_patch_confirms_preserves_omitted_fields_and_verifies(state):
    result = await confirm(
        "update_course_late_policy",
        "42",
        late_submission_deduction=0,
        late_submission_deduction_enabled=False,
    )
    assert result["verified"]
    assert state.policy["late_submission_interval"] == "day"
    assert state.policy["late_submission_deduction"] == 0
    assert state.policy["late_submission_deduction_enabled"] is False
    assert next(
        call for call in state.request.await_args_list if call.args[0] == "patch"
    ).kwargs["data"]["late_policy"] == {
        "late_submission_deduction": 0,
        "late_submission_deduction_enabled": False,
    }


@pytest.mark.parametrize(
    "options",
    [
        {"late_submission_deduction": -1},
        {"missing_submission_deduction": 101},
        {"late_submission_minimum_percent": float("nan")},
        {"late_submission_deduction": float("inf")},
        {"late_submission_interval": "week"},
    ],
)
@pytest.mark.asyncio
async def test_late_policy_bounds_refuse_invalid_values_before_network(state, options):
    result = await (await tool("update_course_late_policy"))("42", **options)
    assert "error" in result
    state.request.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_late_policy_requires_confirmed_creation(state):
    state.policy = None
    result = await confirm(
        "create_course_late_policy",
        "42",
        missing_submission_deduction_enabled=True,
        missing_submission_deduction=50,
    )
    assert result["verified"]
    assert state.policy["missing_submission_deduction"] == 50
    assert state.policy["late_submission_deduction_enabled"] is False


@pytest.mark.asyncio
async def test_late_policy_create_does_not_overwrite_existing(state):
    result = await (await tool("create_course_late_policy"))("42")
    assert "no existing" in result["error"]
    assert all(call.args[0] == "get" for call in state.request.await_args_list)


@pytest.mark.asyncio
async def test_readback_noop_returns_warning_not_success(state):
    state.ignore_write = True
    result = await confirm(
        "update_course_late_policy", "42", late_submission_deduction=5
    )
    assert "warning" in result and not result["verified"]


@pytest.mark.asyncio
async def test_submission_default_is_status_only(state):
    result = await (await tool("get_submission_details"))("42", 7, 11)
    data = result["submission"]
    assert data["attempt"] == 2 and data["score"] == 8
    assert "body" not in data and "user" not in data and "grader_id" not in data
    assert "submission_comments" not in data and "attachments" not in data
    assert state.request.await_args.kwargs["params"] is None


@pytest.mark.asyncio
async def test_submission_optins_are_bounded_fenced_and_exclude_signed_urls(state):
    state.submission["submission_comments"] *= 4
    state.submission["submission_history"] *= 3
    result = await (await tool("get_submission_details"))(
        "42",
        7,
        11,
        include_content=True,
        include_history=True,
        include_comments=True,
        history_limit=1,
        comment_limit=2,
    )
    data = result["submission"]
    assert "SIGNED-SECRET" not in str(result) and "secret@example.test" not in str(
        result
    )
    assert "UNTRUSTED CANVAS CONTENT" in data["body"]
    assert "UNTRUSTED CANVAS CONTENT" in data["submission_comments"][0]["comment"]
    assert len(data["submission_history"]) == 1 and data["history_truncated"]
    assert len(data["submission_comments"]) == 2 and data["comments_truncated"]
    assert state.request.await_args.kwargs["params"] == {
        "include[]": ["submission_history", "submission_comments"]
    }


@pytest.mark.asyncio
async def test_submission_cross_assignment_response_refused(state):
    state.submission["assignment_id"] = 99
    result = await (await tool("get_submission_details"))("42", 7, 11)
    assert "verify" in result["error"]


@pytest.mark.asyncio
async def test_peer_review_unassign_uses_canonical_collection_delete_and_verifies(
    state,
):
    result = await confirm("unassign_peer_review", "42", 7, 20, 12)
    assert result["unassigned"] and result["verified"]
    assert state.request.await_args.args == (
        "delete",
        "/courses/42/assignments/7/submissions/20/peer_reviews",
    )
    assert state.request.await_args.kwargs["params"] == {"user_id": "12"}
    assert not state.reviews


@pytest.mark.asyncio
async def test_peer_review_stale_state_refuses_confirmation(state):
    fn = await tool("unassign_peer_review")
    preview = await fn("42", 7, 20, 12)
    state.reviews[0]["workflow_state"] = "completed"
    result = await fn("42", 7, 20, 12, confirmation_token=preview["confirmation_token"])
    assert "error" in result
    assert all(call.args[0] == "get" for call in state.request.await_args_list)


@pytest.mark.asyncio
async def test_outcome_result_read_uses_bounded_page_filters_and_omits_profiles(state):
    state.outcome_results = [
        {
            "id": 1,
            "score": 3.5,
            "percent": 0.7,
            "links": {
                "user": "11",
                "learning_outcome": "8",
                "alignment": "5",
                "secret": "PRIVATE",
            },
            "user": {"email": "PRIVATE@example.test"},
        }
    ]
    state.outcome_next = (
        "https://canvas.example/api/v1/courses/42/outcome_results?page=3&secret=SECRET"
    )
    result = await (await tool("get_course_outcome_results"))(
        "42", user_ids=[11], outcome_ids=[8], page=2, per_page=10
    )
    assert result["outcome_results"][0]["score"] == 3.5
    assert result["outcome_results"][0]["links"] == {
        "user": "11",
        "learning_outcome": "8",
        "alignment": "5",
    }
    assert "PRIVATE" not in str(result) and "SECRET" not in str(result)
    assert result["has_more"] and result["next_page"] == 3
    params = state.request.await_args.kwargs["params"]
    assert params == {
        "page": 2,
        "per_page": 10,
        "user_ids[]": [11],
        "outcome_ids[]": [8],
        "include_hidden": False,
    }
    assert "include[]" not in params


@pytest.mark.asyncio
async def test_outcome_rollup_read_omits_names_and_keeps_scalar_scores(state):
    state.outcome_rollups = [
        {
            "name": "Jane Doe",
            "links": {"user": 11, "section": 5},
            "scores": [
                {
                    "score": 3,
                    "count": 2,
                    "links": {"outcome": 8},
                    "user": {"email": "PRIVATE@example.test"},
                }
            ],
        }
    ]
    result = await (await tool("get_course_outcome_rollups"))(
        "42", aggregate="course", aggregate_stat="median"
    )
    record = result["rollups"][0]
    assert "Jane Doe" not in str(result) and "PRIVATE" not in str(result)
    assert record["scores"] == [{"score": 3, "count": 2, "links": {"outcome": 8}}]
    assert not result["has_more"] and result["next_page"] is None
    assert state.request.await_args.kwargs["params"]["aggregate_stat"] == "median"


@pytest.mark.parametrize(
    "options",
    [
        {"page": 0},
        {"per_page": 101},
        {"per_page": 0},
        {"user_ids": [0]},
        {"outcome_ids": [-1]},
        {"user_ids": [11, 11]},
        {"outcome_ids": []},
        {"aggregate": "account"},
        {"aggregate_stat": "median"},
    ],
)
@pytest.mark.asyncio
async def test_outcome_rollup_invalid_filters_rejected_before_network(state, options):
    result = await (await tool("get_course_outcome_rollups"))("42", **options)
    assert "error" in result
    state.request.assert_not_awaited()


@pytest.mark.asyncio
async def test_outcome_records_and_scores_have_explicit_bounds(state):
    state.outcome_rollups = [{"scores": [{"score": 3, "count": 1}] * 120}] * 3
    result = await (await tool("get_course_outcome_rollups"))("42", per_page=1)
    assert len(result["rollups"]) == 1 and result["page_truncated"]
    assert len(result["rollups"][0]["scores"]) == 100
    assert result["rollups"][0]["scores_truncated"]
    assert result["rollups"][0]["score_count"] == 120


@pytest.mark.asyncio
async def test_outcome_reads_require_verified_grading_permissions(state):
    state.permission = False
    result = await (await tool("get_course_outcome_results"))("42")
    assert "manage_grades" in result["error"]
    assert len(state.request.await_args_list) == 1


@pytest.mark.parametrize(
    "root,endpoint,payload,created,readback",
    [
        (
            "assignment_override",
            "/courses/42/assignments/7/overrides",
            {"due_at": None},
            {"id": 9},
            {"id": 99, "assignment_id": 7, "due_at": None},
        ),
        (
            "assignment_override",
            "/courses/42/assignments/7/overrides",
            {"due_at": None},
            {"id": 9},
            {"id": 9, "assignment_id": 77, "due_at": None},
        ),
        (
            "assignment_override",
            "/courses/42/assignments/7/overrides",
            {"due_at": None},
            {"id": 9},
            {"id": 9, "assignment_id": 7, "course_id": 99, "due_at": None},
        ),
        (
            "course_section",
            "/courses/42/sections",
            {"name": "New"},
            {"id": 5},
            {"id": 55, "course_id": 42, "name": "New"},
        ),
        (
            "course_section",
            "/courses/42/sections",
            {"name": "New"},
            {"id": 5},
            {"id": 5, "course_id": 99, "name": "New"},
        ),
        (
            "late_policy",
            "/courses/42/late_policy",
            {"late_submission_deduction": 5},
            {},
            {"late_policy": {"course_id": 99, "late_submission_deduction": 5}},
        ),
    ],
)
@pytest.mark.asyncio
async def test_write_readback_checks_identity_even_when_payload_matches(
    state, root, endpoint, payload, created, readback
):
    state.request.side_effect = [created, readback]
    result = await assessment._write_verified(
        "patch" if root == "late_policy" else "post",
        endpoint,
        payload,
        root,
        endpoint if root == "late_policy" else None,
    )
    assert "warning" in result and not result["verified"]
    assert "identity" in result["warning"]


@pytest.mark.asyncio
async def test_completed_peer_reviews_cannot_be_unassigned(state):
    state.reviews[0]["workflow_state"] = "completed"
    result = await (await tool("unassign_peer_review"))("42", 7, 20, 12)
    assert "completed" in result["error"]
    assert all(call.args[0] == "get" for call in state.request.await_args_list)
