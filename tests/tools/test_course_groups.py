from __future__ import annotations

import re
from unittest.mock import AsyncMock, patch

import pytest
from fastmcp import FastMCP

from canvas_mcp.tools import course_groups as groups

CATEGORY = {
    "id": 2,
    "context_type": "Course",
    "course_id": 1,
    "name": "Project groups",
    "role": None,
}
GROUP = {
    "id": 3,
    "context_type": "Course",
    "course_id": 1,
    "group_category_id": 2,
    "name": "Team",
    "description": "Text",
}
MEMBER = {
    "id": 7,
    "group_id": 3,
    "user_id": 9,
    "workflow_state": "accepted",
    "moderator": False,
}


@pytest.fixture
def tools():
    captured = {}
    mcp = FastMCP("groups-test")
    original = mcp.tool

    def tool(*args, **kwargs):
        decorator = original(*args, **kwargs)

        def wrap(fn):
            captured[fn.__name__] = fn
            return decorator(fn)

        return wrap

    mcp.tool = tool
    groups.register_course_group_tools(mcp)
    groups._GUARD.reset()
    return captured


@pytest.fixture
def api():
    state = {
        "category": dict(CATEGORY),
        "group": dict(GROUP),
        "members": [],
        "assignments": [],
        "permission": True,
    }

    async def request(method, endpoint, **kwargs):
        data = kwargs.get("data") or {}
        if endpoint.endswith("/permissions"):
            return dict.fromkeys(kwargs["params"]["permissions[]"], state["permission"])
        if endpoint == "/group_categories/2":
            if method == "put":
                state["category"].update(data)
            return dict(state["category"])
        if endpoint in {"/groups/3", "/group_categories/2/groups"}:
            if method in {"put", "post"}:
                state["group"].update(data)
            return dict(state["group"])
        if endpoint == "/courses/1/group_categories":
            return {**CATEGORY, **data}
        if endpoint == "/groups/3/memberships":
            return MEMBER
        if endpoint == "/groups/3/memberships/7":
            if method == "delete":
                if not state.get("keep_membership_after_delete"):
                    state["members"] = [
                        row for row in state["members"] if row.get("id") != 7
                    ]
                return state.get("delete_ack", {"ok": True})
            return {**MEMBER, **data}
        return {"error": "Unexpected request"}

    async def fetch(endpoint, params=None):
        if endpoint == "/courses/1/group_categories":
            return [state["category"]]
        if endpoint == "/group_categories/2/groups":
            return [state["group"]]
        if endpoint == "/groups/3/memberships":
            return state["members"]
        if endpoint == "/courses/1/assignments":
            return state["assignments"]
        if endpoint == "/courses/1/enrollments":
            return [
                {
                    "user_id": 9,
                    "type": "StudentEnrollment",
                    "enrollment_state": "active",
                }
            ]
        return []

    with (
        patch.object(groups, "get_course_id", AsyncMock(return_value="1")),
        patch.object(
            groups, "make_canvas_request", AsyncMock(side_effect=request)
        ) as req,
        patch.object(
            groups, "fetch_all_paginated_results", AsyncMock(side_effect=fetch)
        ) as pag,
    ):
        yield state, req, pag


def token(preview):
    return re.search(r"Confirmation token: (\S+)", preview).group(1)


async def test_group_reads_fence_text_strip_identifiers(tools, api):
    api[0]["group"]["sis_group_id"] = "private"
    result = await tools["get_course_group"](1, 3)
    assert "UNTRUSTED" in result["group"]["name"]
    assert "sis_group_id" not in result["group"]
    assert (await tools["list_group_categories"](1))["group_categories"][0]["id"] == 2


@pytest.mark.parametrize(
    "field,value", [("course_id", 8), ("context_type", "Account"), ("id", 4)]
)
async def test_cross_course_or_wrong_group_rejected(tools, api, field, value):
    api[0]["group"][field] = value
    assert "error" in await tools["update_course_group"](1, 3, name="New")
    assert all(call.args[0] == "get" for call in api[1].call_args_list)


@pytest.mark.parametrize("permission", [False, None, "true"])
async def test_all_profile_student_cannot_write_groups(tools, api, permission):
    api[0]["permission"] = permission
    assert "permission" in str(await tools["create_group_category"](1, "New"))
    assert all(call.args[0] == "get" for call in api[1].call_args_list)


async def test_preview_confirm_readback_and_replay(tools, api):
    preview = await tools["update_course_group"](1, 3, name="New")
    assert all(call.args[0] == "get" for call in api[1].call_args_list)
    result = await tools["update_course_group"](
        1, 3, name="New", confirmation_token=token(preview)
    )
    assert result["status"] == "updated"
    result = await tools["update_course_group"](
        1, 3, name="New", confirmation_token=token(preview)
    )
    assert "does not match" in result or "already used" in result
    assert len([call for call in api[1].call_args_list if call.args[0] == "put"]) == 1


async def test_changed_group_stops_confirmation(tools, api):
    preview = await tools["update_course_group"](1, 3, name="New")
    api[0]["group"]["description"] = "Changed externally"
    result = await tools["update_course_group"](
        1, 3, name="New", confirmation_token=token(preview)
    )
    assert "does not match" in result
    assert all(call.args[0] == "get" for call in api[1].call_args_list)


@pytest.mark.parametrize(
    "field,value",
    [
        ("sis_group_id", "sis"),
        ("non_collaborative", True),
        ("role", "student_organized"),
    ],
)
async def test_sis_and_special_groups_refused(tools, api, field, value):
    api[0]["group"][field] = value
    assert "known SIS-managed" in str(
        await tools["update_course_group"](1, 3, name="New")
    )


async def test_category_delete_never_cascades(tools, api):
    assert "empty" in str(await tools["delete_group_category"](1, 2))
    assert all(call.args[0] == "get" for call in api[1].call_args_list)


async def test_group_delete_refuses_members_and_linked_assignments(tools, api):
    api[0]["members"] = [MEMBER]
    assert "without members" in str(await tools["delete_course_group"](1, 3))
    api[0]["members"] = []
    api[0]["assignments"] = [
        {"id": 4, "group_category_id": 2, "has_submitted_submissions": False}
    ]
    assert "linked" in str(await tools["delete_course_group"](1, 3))


async def test_empty_group_deletion_confirmed(tools, api):
    preview = await tools["delete_course_group"](1, 3)
    result = await tools["delete_course_group"](1, 3, confirmation_token=token(preview))
    assert result == {"status": "deleted", "id": "3"}


@pytest.mark.parametrize("submitted", [True, None])
async def test_membership_refuses_submitted_or_unknown_work(tools, api, submitted):
    api[0]["assignments"] = [
        {"id": 4, "group_category_id": 2, "has_submitted_submissions": submitted}
    ]
    result = await tools["add_course_group_member"](1, 3, 9)
    assert "student work" in str(result)
    assert all(call.args[0] == "get" for call in api[1].call_args_list)


async def test_membership_add_refuses_implicit_move(tools, api):
    api[0]["members"] = [MEMBER]
    assert "explicitly remove" in str(await tools["add_course_group_member"](1, 3, 9))


async def test_membership_add_and_remove_confirmed(tools, api):
    preview = await tools["add_course_group_member"](1, 3, 9)
    result = await tools["add_course_group_member"](
        1, 3, 9, confirmation_token=token(preview)
    )
    assert result["result"]["user_id"] == 9
    api[0]["members"] = [MEMBER]
    preview = await tools["remove_course_group_member"](1, 3, 9)
    result = await tools["remove_course_group_member"](
        1, 3, 9, confirmation_token=token(preview)
    )
    assert result == {"status": "deleted", "id": 7}
    assert api[1].call_args.args == ("delete", "/groups/3/memberships/7")


async def test_membership_change_rechecks_work_after_preview(tools, api):
    preview = await tools["add_course_group_member"](1, 3, 9)
    api[0]["assignments"] = [
        {"id": 4, "group_category_id": 2, "has_submitted_submissions": True}
    ]
    result = await tools["add_course_group_member"](
        1, 3, 9, confirmation_token=token(preview)
    )
    assert "student work" in str(result)
    assert all(call.args[0] == "get" for call in api[1].call_args_list)


async def test_group_limit_requires_self_signup(tools, api):
    assert "requires self signup" in str(
        await tools["update_group_category"](1, 2, group_limit=3)
    )
    preview = await tools["update_group_category"](
        1, 2, self_signup="enabled", group_limit=3
    )
    result = await tools["update_group_category"](
        1, 2, self_signup="enabled", group_limit=3, confirmation_token=token(preview)
    )
    assert result["status"] == "updated"


async def test_permission_revocation_stops_confirm(tools, api):
    preview = await tools["create_course_group"](1, 2, "New")
    api[0]["permission"] = False
    result = await tools["create_course_group"](
        1, 2, "New", confirmation_token=token(preview)
    )
    assert "permission" in str(result)
    assert all(call.args[0] == "get" for call in api[1].call_args_list)


async def test_membership_delete_acknowledgement_and_readback(tools, api):
    api[0]["members"] = [MEMBER]
    preview = await tools["remove_course_group_member"](1, 3, 9)
    result = await tools["remove_course_group_member"](
        1, 3, 9, confirmation_token=token(preview)
    )
    assert result == {"status": "deleted", "id": 7}
    assert api[2].call_args.args == ("/groups/3/memberships", {"per_page": 100})
    assert api[0]["members"] == []


@pytest.mark.parametrize("ack", [{"ok": False}, {"ok": "true"}, {"id": 7}, {}])
async def test_membership_delete_requires_boolean_ack(tools, api, ack):
    api[0]["members"] = [MEMBER]
    api[0]["delete_ack"] = ack
    preview = await tools["remove_course_group_member"](1, 3, 9)
    result = await tools["remove_course_group_member"](
        1, 3, 9, confirmation_token=token(preview)
    )
    assert result["write_unconfirmed"] is True


async def test_membership_delete_remaining_member_is_unconfirmed(tools, api):
    api[0]["members"] = [MEMBER]
    api[0]["keep_membership_after_delete"] = True
    preview = await tools["remove_course_group_member"](1, 3, 9)
    result = await tools["remove_course_group_member"](
        1, 3, 9, confirmation_token=token(preview)
    )
    assert result["write_unconfirmed"] is True
    assert "not verified" in result["error"]


async def test_category_delete_native_response_confirms_id(tools, api):
    original = api[2].side_effect

    async def empty_category(endpoint, params=None):
        if endpoint == "/group_categories/2/groups":
            return []
        return await original(endpoint, params)

    api[2].side_effect = empty_category
    preview = await tools["delete_group_category"](1, 2)
    result = await tools["delete_group_category"](
        1, 2, confirmation_token=token(preview)
    )
    assert result == {"status": "deleted", "id": "2"}
    assert api[1].call_args.args == ("delete", "/group_categories/2")


async def test_group_update_preserves_sis_stickiness(tools, api):
    preview = await tools["update_course_group"](1, 3, name="New")
    result = await tools["update_course_group"](
        1, 3, name="New", confirmation_token=token(preview)
    )
    assert result["status"] == "updated"
    write = next(call for call in api[1].call_args_list if call.args[0] == "put")
    assert write.kwargs["params"] == {"override_sis_stickiness": "false"}
    assert write.kwargs["data"] == {"name": "New"}


@pytest.mark.parametrize(
    "family", ["discussion_topics", "collaborations", "calendar_events"]
)
async def test_group_delete_refuses_other_group_content(tools, api, family):
    original = api[2].side_effect

    async def content(endpoint, params=None):
        if endpoint.endswith("/" + family) or endpoint == "/" + family:
            if family == "discussion_topics":
                assert params["include_announcements"] is True
            if family == "calendar_events":
                assert params["context_codes[]"] == ["group_3"]
                assert params["all_events"] is True
            return [{"id": 55}]
        return await original(endpoint, params)

    api[2].side_effect = content
    result = await tools["delete_course_group"](1, 3)
    assert "content" in str(result)
    assert all(call.args[0] == "get" for call in api[1].call_args_list)
