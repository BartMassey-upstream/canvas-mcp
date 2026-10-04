import re
from unittest.mock import AsyncMock

import pytest
from fastmcp import FastMCP

from canvas_mcp.core.write_outcome import RequestFailure, WriteOutcome
from canvas_mcp.tools import courses, messaging


async def tool(module, registrar, name):
    mcp = FastMCP("write-confirmation-boundaries")
    getattr(module, registrar)(mcp)
    return (await mcp.get_tool(name)).fn


@pytest.mark.parametrize(
    "response",
    [
        {},
        [],
        None,
        "unexpected",
        [{"id": 1}, {}],
        {"id": 0},
        {"id": True},
        {"id": "malformed"},
    ],
)
async def test_malformed_conversation_response_keeps_unknown_delivery(
    response, monkeypatch
):
    request = AsyncMock(return_value=response)
    monkeypatch.setattr(messaging, "make_canvas_request", request)
    result = await messaging._post_conversation(
        "12", ["7"], "Subject", "Body", False, False, None, "sync", False, None
    )
    assert isinstance(result, RequestFailure)
    assert result.outcome == WriteOutcome.MAY_HAVE_WRITTEN
    assert result.get("success") is not True
    assert result["delivery_uncertain"] is True
    assert result["nothing_sent"] is False
    request.assert_awaited_once()


@pytest.mark.parametrize("response", [{"id": 1}, [{"id": 1}], [{"id": 1}, {"id": "2"}]])
async def test_numeric_conversation_ids_acknowledge_sync_send(response, monkeypatch):
    monkeypatch.setattr(
        messaging, "make_canvas_request", AsyncMock(return_value=response)
    )
    result = await messaging._post_conversation(
        "12", ["7"], "Subject", "Body", False, False, None, "sync", False, None
    )
    assert result["success"] is True


async def test_documented_async_queue_response_does_not_claim_delivery(monkeypatch):
    monkeypatch.setattr(messaging, "make_canvas_request", AsyncMock(return_value=[]))
    result = await messaging._post_conversation(
        "12", ["7", "8"], "Subject", "Body", False, True, None, "async", False, None
    )
    assert result["queued"] is True
    assert result["success"] is False
    assert result["delivery_confirmed"] is False
    assert "sent" not in result


@pytest.mark.parametrize("response", [{}, [], None])
async def test_uncertain_send_cannot_replay_confirmation_token(response, monkeypatch):
    request = AsyncMock(return_value=response)
    monkeypatch.setattr(messaging, "make_canvas_request", request)
    messaging._SEND_CONVERSATION_GUARD.reset()
    send = await tool(
        messaging, "register_educator_messaging_tools", "send_conversation"
    )
    args = {
        "course_identifier": 12,
        "recipient_ids": ["7"],
        "subject": "Subject",
        "body": "Body",
    }
    preview = await send(**args)
    result = await send(**args, confirmation_token=preview["confirmation_token"])
    replay = await send(**args, confirmation_token=preview["confirmation_token"])
    assert result["delivery_uncertain"] is True
    assert replay["nothing_sent"] is True
    request.assert_awaited_once()


@pytest.mark.parametrize(
    "bad_readback", [{"id": 999, "start_at": None}, {"id": 12}, {"start_at": None}, {}]
)
async def test_course_date_clear_requires_course_identity_and_explicit_null(
    bad_readback, monkeypatch
):
    current = {
        "id": 12,
        "start_at": "2026-10-01T00:00:00Z",
        "end_at": None,
        "restrict_enrollments_to_course_dates": True,
    }
    writes = []

    async def request(method, endpoint, **kwargs):
        if method == "put":
            writes.append(kwargs["data"])
            return {"id": 12}
        return bad_readback if writes else dict(current)

    monkeypatch.setattr(courses, "get_course_id", AsyncMock(return_value="12"))
    monkeypatch.setattr(courses, "make_canvas_request", request)
    courses._UPDATE_COURSE_DATES_GUARD.reset()
    update = await tool(
        courses, "register_educator_course_tools", "update_course_dates"
    )
    preview = await update(12, clear_start_at=True)
    token = re.search(r"Confirmation token: (\S+)", preview).group(1)
    result = await update(12, clear_start_at=True, confirmation_token=token)
    assert not isinstance(result, dict) or result.get("updated") is not True
    assert len(writes) == 1


async def test_wrong_course_prewrite_identity_refuses_before_mutation(monkeypatch):
    request = AsyncMock(return_value={"id": 999, "start_at": "2026-10-01T00:00:00Z"})
    monkeypatch.setattr(courses, "get_course_id", AsyncMock(return_value="12"))
    monkeypatch.setattr(courses, "make_canvas_request", request)
    update = await tool(
        courses, "register_educator_course_tools", "update_course_dates"
    )
    result = await update(12, clear_start_at=True)
    assert "identity" in result
    assert all(call.args[0] == "get" for call in request.await_args_list)
