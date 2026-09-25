"""Tests for course-navigation tools."""

from unittest.mock import AsyncMock, patch

import pytest
from fastmcp import FastMCP

from canvas_mcp.tools.navigation import register_navigation_tools


async def _tools():
    mcp = FastMCP("navigation-test")
    register_navigation_tools(mcp)
    return {tool.name: tool.fn for tool in await mcp.list_tools()}


@pytest.mark.asyncio
async def test_list_course_navigation_uses_tabs_endpoint_and_fences_labels():
    with patch(
        "canvas_mcp.tools.navigation.get_course_id", new=AsyncMock(return_value="42")
    ), patch(
        "canvas_mcp.tools.navigation.get_course_code", new=AsyncMock(return_value="ENG101")
    ), patch(
        "canvas_mcp.tools.navigation.make_canvas_request",
        new=AsyncMock(return_value=[{"id": "modules", "label": "Modules"}]),
    ) as request:
        result = await (await _tools())["list_course_navigation"]("ENG101")

    assert request.await_args.args == ("get", "/courses/42/tabs")
    assert "UNTRUSTED CANVAS CONTENT" in result


@pytest.mark.asyncio
async def test_update_course_navigation_preserves_explicit_false():
    with patch(
        "canvas_mcp.tools.navigation.get_course_id", new=AsyncMock(return_value="42")
    ), patch(
        "canvas_mcp.tools.navigation.get_course_code", new=AsyncMock(return_value="ENG101")
    ), patch(
        "canvas_mcp.tools.navigation.make_canvas_request",
        new=AsyncMock(return_value={"id": "modules", "label": "Modules", "hidden": False}),
    ) as request:
        await (await _tools())["update_course_navigation"](
            "ENG101", "modules", position=2, hidden=False
        )

    assert request.await_args.args == ("put", "/courses/42/tabs/modules")
    assert request.await_args.kwargs["data"] == {"position": 2, "hidden": False}


@pytest.mark.asyncio
async def test_update_course_navigation_requires_a_change():
    with patch(
        "canvas_mcp.tools.navigation.get_course_id", new_callable=AsyncMock
    ) as course_id:
        result = await (await _tools())["update_course_navigation"](
            "ENG101", "modules"
        )
    assert "No navigation fields" in result
    course_id.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_course_navigation_rejects_path_shaped_tab_id():
    with patch(
        "canvas_mcp.tools.navigation.get_course_id", new_callable=AsyncMock
    ) as course_id:
        result = await (await _tools())["update_course_navigation"](
            "ENG101", "modules/../users", hidden=True
        )
    assert "Invalid tab_id" in result
    course_id.assert_not_awaited()
