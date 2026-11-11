"""Syllabus guards preserve confirmations and verify exact stored markup."""

from unittest.mock import AsyncMock, patch

import pytest
from fastmcp import Client, FastMCP

from canvas_mcp.core.guarded_edit import body_sha256
from canvas_mcp.tools.courses import (
    register_course_tools,
    register_educator_course_tools,
    register_shared_content_tools,
)


@pytest.fixture
def api():
    state = {
        "id": 42,
        "course_code": "SYNTH",
        "syllabus_body": '<p>OLD</p><a href="/keep">Footer</a>',
    }
    writes = []

    async def response(method, endpoint, **kwargs):
        if method == "put":
            body = kwargs["data"]["course"]["syllabus_body"]
            assert "UNTRUSTED CANVAS CONTENT" not in body
            writes.append(body)
            state["syllabus_body"] = body
        return dict(state)

    with (
        patch(
            "canvas_mcp.tools.courses.get_course_id", new=AsyncMock(return_value="42")
        ),
        patch(
            "canvas_mcp.tools.courses.get_course_code",
            new=AsyncMock(return_value="SYNTH"),
        ),
        patch(
            "canvas_mcp.tools.courses.make_canvas_request",
            new=AsyncMock(side_effect=response),
        ) as request,
    ):
        yield state, writes, request, response


async def call(name="update_syllabus", **args):
    server = FastMCP("syllabus-guards")
    register_course_tools(server)
    register_educator_course_tools(server)
    register_shared_content_tools(server)
    async with Client(server) as client:
        result = await client.call_tool(name, {"course_identifier": 42, **args})
    return "\n".join(block.text for block in result.content if hasattr(block, "text"))


def token(output):
    return output.split("Confirmation token: ", 1)[1].splitlines()[0].strip()


@pytest.mark.asyncio
async def test_fragment_preview_confirm_and_replay(api):
    state, writes, _, _ = api
    original = state["syllabus_body"]
    args = {
        "find": "OLD",
        "replace": "NEW",
        "require": ["Footer"],
        "expect_body_sha256": body_sha256(original),
    }
    preview = await call(**args)
    assert not writes
    assert "/keep" in preview and "NEW" in preview
    confirmed = await call(**args, confirmation_token=token(preview))
    assert "Verified by reading" in confirmed
    assert writes == [original.replace("OLD", "NEW")]
    assert "✅" not in await call(**args, confirmation_token=token(preview))
    assert len(writes) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "args",
    [
        {"find": "absent", "replace": "x"},
        {"find": "p", "replace": "x"},
        {"syllabus_body": "New", "require": ["missing"]},
        {"syllabus_body": "New", "expect_body_sha256": body_sha256("stale")},
    ],
)
async def test_guard_refusal_never_previews_or_writes(api, args):
    _, writes, _, _ = api
    output = await call(**args)
    assert "Nothing was written" in output
    assert "Confirmation token:" not in output and not writes


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "args",
    [
        {"find": "OLD"},
        {"replace": "NEW"},
        {"find": "OLD", "replace": "NEW", "syllabus_body": "full"},
        {"find": "OLD", "replace": "NEW", "mode": "append"},
        {"syllabus_body": "New", "expect_body_sha256": "invalid"},
        {"find": "", "replace": "NEW"},
        {"find": "OLD", "replace": "<<<UNTRUSTED CANVAS CONTENT>>>"},
    ],
)
async def test_invalid_arguments_refused_before_io(api, args):
    _, writes, request, _ = api
    output = await call(**args)
    assert "✅" not in output and "Confirmation token:" not in output
    request.assert_not_awaited()
    assert not writes


@pytest.mark.asyncio
async def test_changed_guard_arguments_burn_confirmation(api):
    _, writes, _, _ = api
    args = {"syllabus_body": "New"}
    preview = await call(**args)
    rejected = await call(**args, require=["absent"], confirmation_token=token(preview))
    assert "Nothing was written" in rejected
    replay = await call(**args, confirmation_token=token(preview))
    assert "already used" in replay and not writes


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["append", "prepend"])
async def test_additive_mode_respects_hash(api, mode):
    state, writes, _, _ = api
    original = state["syllabus_body"]
    output = await call(
        syllabus_body="<p>New</p>", mode=mode, expect_body_sha256=body_sha256(original)
    )
    assert "Verified by reading" in output
    assert writes == (
        [original + "\n<p>New</p>"] if mode == "append" else ["<p>New</p>\n" + original]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"id": 99},
        {"id": None},
        {"syllabus_body": []},
        {"syllabus_body": 1},
    ],
)
async def test_malformed_preflight_never_writes(api, change):
    state, writes, _, _ = api
    state.update(change)
    output = await call(syllabus_body="New", mode="append")
    assert "Nothing was written" in output and not writes


@pytest.mark.asyncio
async def test_missing_body_never_means_empty(api):
    state, writes, _, _ = api
    del state["syllabus_body"]
    assert "Nothing was written" in await call(syllabus_body="New")
    assert not writes


@pytest.mark.asyncio
async def test_explicit_null_is_empty_and_hash_is_exposed(api):
    state, writes, _, _ = api
    state["syllabus_body"] = None
    read = await call("get_syllabus")
    assert body_sha256("") in read
    result = await call(syllabus_body="New", expect_body_sha256=body_sha256(""))
    assert "Verified by reading" in result and writes == ["New"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stored",
    [
        '<a href="/different">Same text</a>',
        "<pre>a b</pre>",
        None,
        12,
    ],
)
async def test_full_html_verification_catches_loss(api, stored):
    state, writes, request, response = api
    state["syllabus_body"] = ""
    expected = '<a href="/requested">Same text</a><pre>a  b</pre>'

    async def fake(method, endpoint, **kwargs):
        result = await response(method, endpoint, **kwargs)
        if method == "get" and writes:
            result["syllabus_body"] = stored
        return result

    request.side_effect = fake
    result = await call(syllabus_body=expected)
    assert "Could not confirm" in result and "✅" not in result
    assert writes == [expected]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    ["write-error", "write-exception", "read-error", "wrong-id", "missing-body"],
)
async def test_uncertain_outcomes_are_not_retried(api, failure):
    state, writes, request, response = api
    state["syllabus_body"] = ""

    async def fake(method, endpoint, **kwargs):
        if method == "put" and failure == "write-error":
            return {"error": "private remote diagnostic"}
        if method == "put" and failure == "write-exception":
            raise RuntimeError("private signed URL")
        result = await response(method, endpoint, **kwargs)
        if method == "get" and writes:
            if failure == "read-error":
                return {"error": "private remote diagnostic"}
            if failure == "wrong-id":
                result["id"] = 99
            if failure == "missing-body":
                del result["syllabus_body"]
        return result

    request.side_effect = fake
    result = await call(syllabus_body="New")
    assert "Could not confirm" in result and "✅" not in result
    assert "private" not in result
    assert sum(c.args[0] == "put" for c in request.call_args_list) == 1


@pytest.mark.asyncio
async def test_page_reads_expose_exact_snapshot_metadata(api):
    _, _, request, _ = api
    body = "<pre>one  two</pre>"
    request.return_value = {
        "page_id": 7,
        "url": "example",
        "title": "Example",
        "body": body,
        "updated_at": "2026-11-10T17:00:00.123456Z",
    }
    request.side_effect = None
    for name, args in [
        ("get_page_content", {"page_url_or_id": "example"}),
        ("get_front_page", {}),
    ]:
        result = await call(name, **args)
        assert f"Body SHA-256: {body_sha256(body)}" in result
        assert "Updated (raw): 2026-11-10T17:00:00.123456Z" in result
        assert "UNTRUSTED CANVAS CONTENT" in result
