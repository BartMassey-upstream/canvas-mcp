"""Course-navigation tools that do not access student data."""

import re
from typing import Any

from fastmcp import FastMCP
from mcp.types import ToolAnnotations

from ..core.cache import get_course_code, get_course_id
from ..core.client import make_canvas_request
from ..core.path import canvas_path
from ..core.untrusted_content import fence_untrusted_inline
from ..core.validation import validate_params

_TAB_ID = re.compile(r"^[A-Za-z0-9_-]+$")


def _format_tab(tab: dict[str, Any]) -> str:
    return "\n".join(
        [
            f"ID: {tab.get('id')}",
            f"Label: {fence_untrusted_inline(tab.get('label') or 'Unnamed tab', 'navigation tab label')}",
            f"Type: {tab.get('type', 'N/A')}",
            f"Position: {tab.get('position', 'N/A')}",
            f"Hidden: {tab.get('hidden', False)}",
        ]
    )


def register_navigation_tools(mcp: FastMCP) -> None:
    """Register course-navigation listing and settings updates."""

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    @validate_params
    async def list_course_navigation(course_identifier: str | int) -> str:
        """List the tabs in a course's left-hand navigation."""
        course_id = await get_course_id(course_identifier)
        tabs = await make_canvas_request("get", canvas_path('courses', course_id, 'tabs'))
        if isinstance(tabs, dict) and "error" in tabs:
            return f"Error listing course navigation: {tabs['error']}"
        if not tabs:
            return f"No navigation tabs found for course {course_identifier}."
        course_display = await get_course_code(course_id) or course_identifier
        return f"Course navigation for {course_display}:\n\n" + "\n\n".join(
            _format_tab(tab) for tab in tabs
        )

    @mcp.tool(annotations=ToolAnnotations(destructive_hint=True, idempotent_hint=True))
    @validate_params
    async def update_course_navigation(
        course_identifier: str | int,
        tab_id: str,
        position: int | None = None,
        hidden: bool | None = None,
    ) -> str:
        """Change a course navigation tab's position or visibility."""
        if not _TAB_ID.fullmatch(tab_id):
            return "Invalid tab_id. Use the exact ID returned by list_course_navigation."
        if position is None and hidden is None:
            return "No navigation fields were provided to update."
        if position is not None and position < 1:
            return "Invalid position. Navigation positions start at 1."

        data: dict[str, int | bool] = {}
        if position is not None:
            data["position"] = position
        if hidden is not None:
            data["hidden"] = hidden

        course_id = await get_course_id(course_identifier)
        tab = await make_canvas_request(
            "put", canvas_path('courses', course_id, 'tabs', tab_id), data=data
        )
        if "error" in tab:
            return f"Error updating course navigation: {tab['error']}"
        course_display = await get_course_code(course_id) or course_identifier
        return f"Navigation updated for course {course_display}:\n\n{_format_tab(tab)}"
