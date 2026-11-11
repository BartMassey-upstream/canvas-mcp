"""Exercise registered discussion and announcement edits against raw snapshots."""

from unittest.mock import AsyncMock, patch

import pytest
from fastmcp import FastMCP

from canvas_mcp.core.guarded_edit import body_sha256
from canvas_mcp.tools.discussions import (
    register_educator_discussion_tools,
    register_shared_discussion_tools,
)

RAW = '<p><a href="https://example.org/a?x=1&amp;y=2">old</a></p><pre> a\n  b </pre>'


async def tool(name):
    mcp = FastMCP("discussion-guards")
    register_shared_discussion_tools(mcp)
    register_educator_discussion_tools(mcp)
    return (await mcp.get_tool(name)).fn


@pytest.fixture(params=["update_discussion_topic", "update_announcement"])
def edit(request):
    return request.param


def topic(**overrides):
    return {"id": 7, "title": "Prompt", "message": RAW,
            "is_announcement": True, **overrides}


async def invoke(name, responses, **kwargs):
    with patch("canvas_mcp.tools.discussions.get_course_id", AsyncMock(return_value="42")), patch(
        "canvas_mcp.tools.discussions.make_canvas_request", AsyncMock(side_effect=responses)
    ) as request:
        result = await (await tool(name))(42, 7, **kwargs)
    return result, request


@pytest.mark.asyncio
async def test_exact_fragment_preserves_links_and_pre_whitespace(edit):
    expected = RAW.replace('>old<', '>new<')
    result, request = await invoke(
        edit, [topic(), {}, topic(message=expected)],
        find=">old<", replace=">new<", expect_body_sha256=body_sha256(RAW),
        require=["<pre> a\n  b </pre>", 'href="https://example.org/a?x=1&amp;y=2"'],
    )
    assert "confirmed by reading" in result
    assert [call.args[0] for call in request.await_args_list] == ["get", "put", "get"]
    assert request.await_args_list[1].kwargs["data"] == {"message": expected}


@pytest.mark.asyncio
@pytest.mark.parametrize("kwargs", [
    {"message": "new", "expect_body_sha256": body_sha256("stale")},
    {"find": "missing", "replace": "new"},
    {"find": "a", "replace": "new"},
    {"message": "new", "require": ["missing"]},
])
async def test_failed_preconditions_do_not_write(edit, kwargs):
    result, request = await invoke(edit, [topic()], **kwargs)
    assert "Nothing was written" in result
    assert request.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("kwargs", [
    {"message": "new", "find": "old", "replace": "new"},
    {"find": "old"}, {"expect_body_sha256": "bad", "published": True},
])
async def test_invalid_arguments_do_not_fetch(edit, kwargs):
    result, request = await invoke(edit, [], **kwargs)
    assert "Nothing was written" in result
    request.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [{"id": 8, "message": RAW}, {"id": 7}])
async def test_wrong_identity_or_missing_body_refuses_full_write(edit, bad):
    result, request = await invoke(edit, [{**bad, "is_announcement": True}], message="new")
    assert "Nothing was written" in result
    assert request.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("after", [topic(message="sanitized"), {"error": "unavailable"}, topic(id=8)])
async def test_readback_failure_never_reports_success(edit, after):
    result, request = await invoke(edit, [topic(), {}, after], message="new")
    assert "unconfirmed" in result.lower()
    assert request.await_count == 3


@pytest.mark.asyncio
async def test_uncertain_put_is_never_retried(edit):
    result, request = await invoke(edit, [topic(), RuntimeError("connection lost")], message="new")
    assert "Do not retry automatically" in result
    assert request.await_count == 2


@pytest.mark.asyncio
async def test_metadata_only_and_null_date_clear(edit):
    result, request = await invoke(edit, [topic(), {}, topic(lock_at=None, title="Renamed")],
                                   title="Renamed", clear_lock_at=True)
    assert "confirmed by reading" in result
    assert request.await_args_list[1].kwargs["data"] == {"title": "Renamed", "lock_at": ""}


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["before", "after"])
@pytest.mark.parametrize("flag", [False, None, 1, "true"])
async def test_announcement_type_must_be_positive_boolean(stage, flag):
    bad = topic(is_announcement=flag)
    responses = [bad] if stage == "before" else [topic(), {}, bad]
    result, request = await invoke("update_announcement", responses, message="new")
    assert "confirmed by reading" not in result
    assert request.await_count == (1 if stage == "before" else 3)


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["get_discussion_topic_details", "get_discussion_with_replies", "get_announcement"])
async def test_raw_topic_hash_is_exposed_outside_fence(name):
    with patch("canvas_mcp.tools.discussions.get_course_id", AsyncMock(return_value="42")), patch(
        "canvas_mcp.tools.discussions.get_course_code", AsyncMock(return_value="CS")
    ), patch("canvas_mcp.tools.discussions.make_canvas_request", AsyncMock(return_value=topic())), patch(
        "canvas_mcp.tools.discussions.fetch_all_paginated_results",
        AsyncMock(return_value=[topic(user_name="Reader")]),
    ):
        result = await (await tool(name))(42, 7)
    assert f"Body SHA-256: {body_sha256(RAW)}" in result
    prefix = result.split("Body SHA-256:", 1)[0]
    assert prefix.count("<<<UNTRUSTED CANVAS CONTENT") == prefix.count("<<<END UNTRUSTED CANVAS CONTENT>>>")


@pytest.mark.asyncio
async def test_metadata_only_verifies_body_and_requested_fields(edit):
    for after in (topic(message=RAW + "drift", title="Renamed"), topic(title="Wrong")):
        result, request = await invoke(edit, [topic(), {}, after], title="Renamed")
        assert "unconfirmed" in result.lower()
        assert request.await_args_list[1].kwargs["data"] == {"title": "Renamed"}


@pytest.mark.asyncio
async def test_empty_body_hash_and_clear_message(edit):
    result, request = await invoke(edit, [topic(message=None), {}, topic(message=None)],
                                   message="", expect_body_sha256=body_sha256(None))
    assert "confirmed by reading" in result
    assert request.await_args_list[1].kwargs["data"] == {"message": ""}
