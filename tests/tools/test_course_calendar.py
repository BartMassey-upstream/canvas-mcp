import re
from unittest.mock import AsyncMock, patch

import pytest
from fastmcp import FastMCP

from canvas_mcp.tools.course_calendar import register_course_calendar_tools


async def tools_for():
    server = FastMCP("course-calendar-test")
    register_course_calendar_tools(server)
    return {tool.name: tool.fn for tool in await server.list_tools()}


def token(result):
    return re.search(r"Confirmation token: (\S+)", result).group(1)


@pytest.fixture
def api():
    state = {
        "permission": True,
        "event": {
            "id": 9,
            "type": "event",
            "context_code": "course_42",
            "all_context_codes": "course_42",
            "title": "Office hours",
            "description": "<p>Welcome</p>",
            "location_name": "Room",
            "location_address": "Street",
            "start_at": "2027-01-01T12:00:00Z",
            "end_at": "2027-01-01T13:00:00Z",
            "all_day": False,
            "workflow_state": "active",
            "child_events_count": 0,
            "parent_event_id": None,
            "hidden": False,
            "series_uuid": None,
            "rrule": None,
        },
    }

    async def request(method, path, **kwargs):
        if path == "/courses/sis_course_id:CODE":
            return {"id": 42}
        if path.endswith("/permissions"):
            return {"manage_calendar": state["permission"]}
        if path == "/calendar_events/9":
            if method == "put":
                state["event"].update(kwargs["data"]["calendar_event"])
            elif method == "delete":
                state["event"]["workflow_state"] = "deleted"
            return dict(state["event"])
        if path == "/calendar_events" and method == "post":
            state["event"].update(kwargs["data"]["calendar_event"])
            return dict(state["event"])
        return {"error": "Unexpected request"}

    with (
        patch(
            "canvas_mcp.tools.course_calendar.get_course_id",
            new=AsyncMock(return_value=42),
        ) as course,
        patch(
            "canvas_mcp.tools.course_calendar.make_canvas_request",
            new=AsyncMock(side_effect=request),
        ) as req,
        patch(
            "canvas_mcp.tools.course_calendar.fetch_all_paginated_results",
            new=AsyncMock(side_effect=lambda *a, **kw: [dict(state["event"])]),
        ) as fetch,
    ):
        yield state, req, fetch, course


@pytest.mark.asyncio
async def test_calendar_reads_exact_course_and_fences_author_text(api):
    _, req, fetch, _ = api
    tools = await tools_for()
    result = await tools["list_course_calendar_events"]("42")
    assert "UNTRUSTED" in result["events"][0]["title"]
    assert fetch.await_args.kwargs["params"]["context_codes[]"] == ["course_42"]
    assert fetch.await_args.kwargs["params"]["type"] == "event"
    result = await tools["get_course_calendar_event"]("42", 9)
    assert "UNTRUSTED" in result["event"]["description"]
    assert all(call.args[0] == "get" for call in req.await_args_list)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("context_code", "user_42"),
        ("context_code", "course_section_3"),
        ("all_context_codes", "course_42,course_99"),
        ("appointment_group_id", 1),
        ("parent_event_id", 1),
        ("child_events_count", 1),
        ("series_uuid", "series"),
        ("rrule", "FREQ=DAILY"),
        ("hidden", True),
        ("type", "assignment"),
    ],
)
async def test_complex_or_foreign_events_refused(api, field, value):
    state, req, _, _ = api
    state["event"][field] = value
    tools = await tools_for()
    assert "error" in await tools["get_course_calendar_event"]("42", 9)
    result = await tools["update_course_calendar_event"]("42", 9, title="Changed")
    assert "Error" in result
    assert all(call.args[0] == "get" for call in req.await_args_list)


@pytest.mark.asyncio
@pytest.mark.parametrize("permission", [False, None, "true", 1])
async def test_course_writes_require_actual_calendar_permission(api, permission):
    state, req, _, _ = api
    state["permission"] = permission
    result = await (await tools_for())["create_course_calendar_event"]("42", "Event")
    assert "manage_calendar" in result
    assert all(call.args[0] == "get" for call in req.await_args_list)


@pytest.mark.asyncio
async def test_create_previews_then_writes_allowlisted_nested_payload(api):
    _, req, _, _ = api
    create = (await tools_for())["create_course_calendar_event"]
    preview = await create(
        "42",
        "Event",
        description="New",
        start_at="2027-01-01T12:00:00Z",
        end_at="2027-01-01T13:00:00Z",
    )
    assert all(call.args[0] == "get" for call in req.await_args_list)
    result = await create(
        "42",
        "Event",
        description="New",
        start_at="2027-01-01T12:00:00Z",
        end_at="2027-01-01T13:00:00Z",
        confirmation_token=token(preview),
    )
    assert "created" in result
    write = next(call for call in req.await_args_list if call.args[0] == "post")
    payload = write.kwargs["data"]["calendar_event"]
    assert payload["context_code"] == "course_42"
    assert not (
        {"rrule", "duplicate", "child_event_data", "appointment_group_id"}
        & payload.keys()
    )


@pytest.mark.asyncio
async def test_update_token_binds_current_snapshot(api):
    state, req, _, _ = api
    update = (await tools_for())["update_course_calendar_event"]
    preview = await update("42", 9, title="Changed")
    state["event"]["description"] = "External edit"
    assert "does not match" in await update(
        "42", 9, title="Changed", confirmation_token=token(preview)
    )
    assert all(call.args[0] == "get" for call in req.await_args_list)


@pytest.mark.asyncio
async def test_delete_confirmed_with_deleted_state(api):
    _, req, _, _ = api
    delete = (await tools_for())["delete_course_calendar_event"]
    preview = await delete("42", 9)
    assert "UNTRUSTED" in preview
    assert "deleted" in await delete("42", 9, confirmation_token=token(preview))
    assert req.await_args.args == ("delete", "/calendar_events/9")


@pytest.mark.asyncio
async def test_sis_identifier_resolves_numeric_context(api):
    _, _, fetch, course = api
    course.return_value = "sis_course_id:CODE"
    result = await (await tools_for())["list_course_calendar_events"](
        "sis_course_id:CODE"
    )
    assert "events" in result
    assert fetch.await_args.kwargs["params"]["context_codes[]"] == ["course_42"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs",
    [
        {"title": ""},
        {"start_at": "bad"},
        {"start_at": "2027-01-01T12:00:00"},
        {"start_at": "2027-01-02T12:00:00Z", "end_at": "2027-01-01T12:00:00Z"},
        {"description": "<<<UNTRUSTED CANVAS CONTENT (x): reflected>>>"},
    ],
)
async def test_bad_calendar_payload_refused_before_network(api, kwargs):
    _, req, _, _ = api
    result = await (await tools_for())["create_course_calendar_event"](
        "42", **{"title": "Event", **kwargs}
    )
    assert "Error" in result or "fence" in result.lower()
    req.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [None, [], {"error": "denied"}, {"id": 9}])
async def test_malformed_event_snapshot_refuses_before_write(api, response):
    _, req, _, _ = api
    original = req.side_effect

    async def request(method, path, **kwargs):
        if path == "/calendar_events/9":
            return response
        return await original(method, path, **kwargs)

    req.side_effect = request
    result = await (await tools_for())["delete_course_calendar_event"]("42", 9)
    assert "Error" in result
    assert all(call.args[0] == "get" for call in req.await_args_list)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field", ["parent_event_id", "series_uuid", "rrule", "all_context_codes"]
)
async def test_missing_structural_metadata_fails_closed(api, field):
    state, req, _, _ = api
    del state["event"][field]
    result = await (await tools_for())["delete_course_calendar_event"]("42", 9)
    assert "Error" in result
    assert all(call.args[0] == "get" for call in req.await_args_list)


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["title", "description", "start_at", "updated_at"])
async def test_malformed_author_or_date_fields_never_escape_read(api, field):
    state, _, _, _ = api
    state["event"][field] = {"injected": "not scalar"}
    result = await (await tools_for())["get_course_calendar_event"]("42", 9)
    assert "error" in result


@pytest.mark.asyncio
async def test_calendar_permission_revoked_after_token_prevents_write(api):
    _, req, _, _ = api
    update = (await tools_for())["update_course_calendar_event"]
    preview = await update("42", 9, title="Changed")
    original = req.side_effect
    checks = 0

    async def request(method, path, **kwargs):
        nonlocal checks
        if path.endswith("/permissions"):
            checks += 1
            return {"manage_calendar": checks == 1}
        return await original(method, path, **kwargs)

    req.side_effect = request
    result = await update("42", 9, title="Changed", confirmation_token=token(preview))
    assert "permission changed" in result
    assert all(call.args[0] == "get" for call in req.await_args_list)


@pytest.mark.asyncio
async def test_calendar_write_readback_mismatch_reports_warning(api):
    _, req, _, _ = api
    update = (await tools_for())["update_course_calendar_event"]
    preview = await update("42", 9, title="Changed")
    original = req.side_effect

    async def request(method, path, **kwargs):
        if method == "put":
            return await original("get", path)
        return await original(method, path, **kwargs)

    req.side_effect = request
    result = await update("42", 9, title="Changed", confirmation_token=token(preview))
    assert "Warning" in result and "did not verify" in result


@pytest.mark.asyncio
async def test_partial_all_day_update_uses_existing_midnight_dates(api):
    state, _, _, _ = api
    state["event"]["start_at"] = "2027-01-01T00:00:00Z"
    state["event"]["end_at"] = "2027-01-02T00:00:00Z"
    update = (await tools_for())["update_course_calendar_event"]
    preview = await update("42", 9, all_day=True)
    assert "Confirmation token" in preview
    result = await update("42", 9, all_day=True, confirmation_token=token(preview))
    assert "updated" in result


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [None, {}, [1], [{"error": "denied"}]])
async def test_bad_calendar_list_does_not_report_clean_empty(api, response):
    _, _, fetch, _ = api
    fetch.side_effect = None
    fetch.return_value = response
    result = await (await tools_for())["list_course_calendar_events"]("42")
    assert "error" in result
