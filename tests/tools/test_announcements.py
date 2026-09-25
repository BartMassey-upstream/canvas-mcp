"""Tests for announcement-only detail and update tools."""

from unittest.mock import AsyncMock, patch

import pytest
from fastmcp import FastMCP

from canvas_mcp.tools.discussions import register_educator_discussion_tools


async def _tools():
    mcp = FastMCP("announcement-test")
    register_educator_discussion_tools(mcp)
    return {tool.name: tool.fn for tool in await mcp.list_tools()}


@pytest.mark.asyncio
async def test_get_announcement_uses_only_announcement_collection():
    announcement = {
        "id": 7,
        "title": "Welcome",
        "message": "Read the syllabus",
        "is_announcement": True,
    }
    with patch(
        "canvas_mcp.tools.discussions.get_course_id", new=AsyncMock(return_value="42")
    ), patch(
        "canvas_mcp.tools.discussions.fetch_all_paginated_results",
        new=AsyncMock(return_value=[announcement]),
    ) as fetch, patch(
        "canvas_mcp.tools.discussions.make_canvas_request", new_callable=AsyncMock
    ) as request:
        result = await (await _tools())["get_announcement"]("ENG101", 7)

    assert fetch.await_args.args == (
        "/courses/42/discussion_topics",
        {"only_announcements": True, "per_page": 100},
    )
    assert result.count("UNTRUSTED CANVAS CONTENT") >= 4
    request.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_announcement_rejects_an_id_absent_from_announcement_listing():
    ordinary_discussion = {"id": 8, "title": "Student discussion"}
    with patch(
        "canvas_mcp.tools.discussions.get_course_id", new=AsyncMock(return_value="42")
    ), patch(
        "canvas_mcp.tools.discussions.fetch_all_paginated_results",
        new=AsyncMock(return_value=[ordinary_discussion]),
    ), patch(
        "canvas_mcp.tools.discussions.make_canvas_request", new_callable=AsyncMock
    ) as request:
        result = await (await _tools())["get_announcement"]("ENG101", 9)

    assert "not found" in result
    request.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_announcement_verifies_before_writing():
    announcement = {"id": 7, "title": "Welcome", "is_announcement": True}
    updated = {
        "id": 7,
        "title": "Start here",
        "message": "Read the syllabus",
        "is_announcement": True,
    }
    with patch(
        "canvas_mcp.tools.discussions.get_course_id", new=AsyncMock(return_value="42")
    ), patch(
        "canvas_mcp.tools.discussions.fetch_all_paginated_results",
        new=AsyncMock(return_value=[announcement]),
    ), patch(
        "canvas_mcp.tools.discussions.make_canvas_request",
        new=AsyncMock(return_value=updated),
    ) as request:
        result = await (await _tools())["update_announcement"](
            "ENG101", 7, title="Start here", published=False
        )

    assert request.await_args.args == (
        "put",
        "/courses/42/discussion_topics/7",
    )
    assert request.await_args.kwargs["data"] == {
        "title": "Start here",
        "published": False,
    }
    assert "updated" in result


@pytest.mark.asyncio
async def test_update_announcement_never_writes_unverified_id():
    with patch(
        "canvas_mcp.tools.discussions.get_course_id", new=AsyncMock(return_value="42")
    ), patch(
        "canvas_mcp.tools.discussions.fetch_all_paginated_results",
        new=AsyncMock(return_value=[]),
    ), patch(
        "canvas_mcp.tools.discussions.make_canvas_request", new_callable=AsyncMock
    ) as request:
        result = await (await _tools())["update_announcement"](
            "ENG101", 9, title="Not a discussion"
        )

    assert "not found" in result
    request.assert_not_awaited()
