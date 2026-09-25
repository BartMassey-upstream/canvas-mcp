"""Tests for assignment-group course-construction tools."""

import re
from unittest.mock import AsyncMock, patch

import pytest
from fastmcp import FastMCP

from canvas_mcp.tools.assignment_groups import register_assignment_group_tools


async def _tool(name: str):
    mcp = FastMCP("assignment-groups-test")
    register_assignment_group_tools(mcp)
    tools = {tool.name: tool.fn for tool in await mcp.list_tools()}
    return tools[name]


@pytest.mark.asyncio
async def test_list_assignment_groups_uses_no_student_includes():
    with patch(
        "canvas_mcp.tools.assignment_groups.get_course_id", new=AsyncMock(return_value="42")
    ), patch(
        "canvas_mcp.tools.assignment_groups.get_course_code", new=AsyncMock(return_value="ENG101")
    ), patch(
        "canvas_mcp.tools.assignment_groups.fetch_all_paginated_results",
        new=AsyncMock(return_value=[{"id": 7, "name": "Projects", "position": 1}]),
    ) as fetch:
        result = await (await _tool("list_assignment_groups"))("ENG101")

    assert fetch.await_args.args == (
        "/courses/42/assignment_groups",
        {"per_page": 100},
    )
    assert "UNTRUSTED CANVAS CONTENT" in result


@pytest.mark.asyncio
async def test_create_assignment_group_sends_only_course_structure_fields():
    with patch(
        "canvas_mcp.tools.assignment_groups.get_course_id", new=AsyncMock(return_value="42")
    ), patch(
        "canvas_mcp.tools.assignment_groups.get_course_code", new=AsyncMock(return_value="ENG101")
    ), patch(
        "canvas_mcp.tools.assignment_groups.make_canvas_request",
        new=AsyncMock(return_value={"id": 7, "name": "Projects", "position": 2, "group_weight": 30}),
    ) as request:
        await (await _tool("create_assignment_group"))(
            "ENG101", "Projects", position=2, group_weight=30
        )

    assert request.await_args.kwargs["data"] == {
        "name": "Projects",
        "position": 2,
        "group_weight": 30,
    }


@pytest.mark.asyncio
async def test_update_assignment_group_requires_a_change():
    with patch(
        "canvas_mcp.tools.assignment_groups.get_course_id", new_callable=AsyncMock
    ) as course_id:
        result = await (await _tool("update_assignment_group"))("ENG101", 7)
    assert "No assignment-group fields" in result
    course_id.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_assignment_group_moves_assignments_after_confirmation():
    source = {"id": 7, "name": "Old group"}
    target = {"id": 8, "name": "New group"}
    with patch(
        "canvas_mcp.tools.assignment_groups.get_course_id", new=AsyncMock(return_value="42")
    ), patch(
        "canvas_mcp.tools.assignment_groups.get_course_code", new=AsyncMock(return_value="ENG101")
    ), patch(
        "canvas_mcp.tools.assignment_groups.make_canvas_request",
        new=AsyncMock(side_effect=[source, target, source, target, source]),
    ) as request:
        delete = await _tool("delete_assignment_group")
        preview = await delete("ENG101", 7, 8)
        token = re.search(r"Confirmation token: (\S+)", preview).group(1)
        result = await delete("ENG101", 7, 8, confirmation_token=token)

    assert "No assignments would be deleted" in preview
    assert "were moved" in result
    assert request.await_args.args == (
        "delete",
        "/courses/42/assignment_groups/7",
    )
    assert request.await_args.kwargs["params"] == {"move_assignments_to": "8"}
