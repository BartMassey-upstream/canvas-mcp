"""Assignment-group tools for course construction."""

from typing import Any

from fastmcp import FastMCP
from mcp.types import ToolAnnotations

from ..core.cache import get_course_code, get_course_id
from ..core.client import fetch_all_paginated_results, make_canvas_request
from ..core.path import canvas_path
from ..core.untrusted_content import (
    FENCE_LEAK_ERROR,
    contains_fence_markers,
    fence_untrusted_inline,
)
from ..core.validation import validate_params
from ..core.write_confirmation import (
    ConfirmationGuard,
    preview_with_token,
    redeem_confirmation,
)

_DELETE_ASSIGNMENT_GROUP_GUARD = ConfirmationGuard(
    nothing_done="The assignment group was not deleted."
)


def _format_group(
    group: dict[str, Any], *, include_assignments: bool = False
) -> str:
    name = fence_untrusted_inline(
        group.get("name") or "Unnamed assignment group",
        "assignment group name",
    )
    result = (
        f"ID: {group.get('id')}\n"
        f"Name: {name}\n"
        f"Position: {group.get('position', 'N/A')}\n"
        f"Weight: {group.get('group_weight', 0)}%"
    )
    if not include_assignments:
        return result

    assignments = group.get("assignments") or []
    if not assignments:
        return result + "\nAssignments: none"

    lines = [result, "Assignments:"]
    for assignment in assignments:
        assignment_name = fence_untrusted_inline(
            assignment.get("name") or "Unnamed assignment",
            "assignment name",
        )
        quiz_id = assignment.get("quiz_id")
        kind = (
            "Classic Quiz"
            if quiz_id is not None
            or "online_quiz" in (assignment.get("submission_types") or [])
            else "Assignment"
        )
        identity = f"assignment ID {assignment.get('id')}"
        if quiz_id is not None:
            identity += f", Classic Quiz ID {quiz_id}"
        lines.append(f"  - {assignment_name} ({kind}; {identity})")
    return "\n".join(lines)


def register_assignment_group_tools(mcp: FastMCP) -> None:
    """Register assignment-group management without student-level includes."""

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    @validate_params
    async def list_assignment_groups(
        course_identifier: str | int,
        include_assignments: bool = False,
    ) -> str:
        """List groups, optionally with assignment definitions; never student data."""
        course_id = await get_course_id(course_identifier)
        params: dict[str, Any] = {"per_page": 100}
        if include_assignments:
            params["include[]"] = ["assignments"]
        groups = await fetch_all_paginated_results(
            canvas_path('courses', course_id, 'assignment_groups'),
            params,
        )
        if isinstance(groups, dict) and "error" in groups:
            return f"Error listing assignment groups: {groups['error']}"
        if not groups:
            return f"No assignment groups found for course {course_identifier}."

        course_display = await get_course_code(course_id) or course_identifier
        return (
            f"Assignment groups for course {course_display}:\n\n"
            + "\n\n".join(
                _format_group(group, include_assignments=include_assignments)
                for group in groups
            )
        )

    @mcp.tool(annotations=ToolAnnotations(destructive_hint=False, idempotent_hint=False))
    @validate_params
    async def create_assignment_group(
        course_identifier: str | int,
        name: str,
        position: int | None = None,
        group_weight: float | None = None,
    ) -> str:
        """Create an assignment group for a course."""
        if contains_fence_markers(name):
            return FENCE_LEAK_ERROR
        data: dict[str, str | int | float] = {"name": name}
        if position is not None:
            data["position"] = position
        if group_weight is not None:
            data["group_weight"] = group_weight

        course_id = await get_course_id(course_identifier)
        response = await make_canvas_request(
            "post", canvas_path('courses', course_id, 'assignment_groups'), data=data
        )
        if "error" in response:
            return f"Error creating assignment group: {response['error']}"

        course_display = await get_course_code(course_id) or course_identifier
        return (
            f"Assignment group created in course {course_display}:\n\n"
            f"{_format_group(response)}"
        )

    @mcp.tool(annotations=ToolAnnotations(destructive_hint=False, idempotent_hint=True))
    @validate_params
    async def update_assignment_group(
        course_identifier: str | int,
        assignment_group_id: str | int,
        name: str | None = None,
        position: int | None = None,
        group_weight: float | None = None,
    ) -> str:
        """Update an assignment group's name, position, or weight."""
        if name is not None and contains_fence_markers(name):
            return FENCE_LEAK_ERROR
        data: dict[str, str | int | float] = {}
        if name is not None:
            data["name"] = name
        if position is not None:
            data["position"] = position
        if group_weight is not None:
            data["group_weight"] = group_weight
        if not data:
            return "No assignment-group fields were provided to update."

        course_id = await get_course_id(course_identifier)
        response = await make_canvas_request(
            "put",
            canvas_path('courses', course_id, 'assignment_groups', assignment_group_id),
            data=data,
        )
        if "error" in response:
            return f"Error updating assignment group: {response['error']}"

        course_display = await get_course_code(course_id) or course_identifier
        return (
            f"Assignment group updated in course {course_display}:\n\n"
            f"{_format_group(response)}"
        )

    @mcp.tool(annotations=ToolAnnotations(destructive_hint=True, idempotent_hint=True))
    @validate_params
    async def delete_assignment_group(
        course_identifier: str | int,
        assignment_group_id: str | int,
        move_assignments_to: str | int,
        confirmation_token: str | None = None,
    ) -> str:
        """Delete a group after preview, moving its assignments to another group."""
        course_id = await get_course_id(course_identifier)
        source = await make_canvas_request(
            "get",
            canvas_path('courses', course_id, 'assignment_groups', assignment_group_id),
            params={"override_assignment_dates": False},
        )
        if "error" in source:
            return f"Error fetching assignment group: {source['error']}"
        target = await make_canvas_request(
            "get",
            canvas_path('courses', course_id, 'assignment_groups', move_assignments_to),
            params={"override_assignment_dates": False},
        )
        if "error" in target:
            return f"Error fetching destination assignment group: {target['error']}"
        if str(assignment_group_id) == str(move_assignments_to):
            return "The destination assignment group must differ from the group being deleted."

        source_name = source.get("name") or "Unnamed assignment group"
        target_name = target.get("name") or "Unnamed assignment group"
        shown_source = fence_untrusted_inline(source_name, "assignment group name")
        shown_target = fence_untrusted_inline(target_name, "assignment group name")
        fingerprint = _DELETE_ASSIGNMENT_GROUP_GUARD.fingerprint(
            "delete_assignment_group",
            str(course_id),
            str(assignment_group_id),
            source_name,
            str(move_assignments_to),
            target_name,
        )
        if not confirmation_token:
            preview = (
                f"Would delete assignment group **{shown_source}**.\n"
                f"All assignments in it would move to **{shown_target}**.\n"
                "No assignments would be deleted."
            )
            return preview_with_token(
                _DELETE_ASSIGNMENT_GROUP_GUARD,
                fingerprint,
                "delete_assignment_group",
                preview,
            )
        error = redeem_confirmation(
            _DELETE_ASSIGNMENT_GROUP_GUARD,
            confirmation_token,
            fingerprint,
        )
        if error:
            return error

        response = await make_canvas_request(
            "delete",
            canvas_path('courses', course_id, 'assignment_groups', assignment_group_id),
            params={"move_assignments_to": move_assignments_to},
        )
        if isinstance(response, dict) and "error" in response:
            return f"Error deleting assignment group: {response['error']}"
        return (
            f"Assignment group **{shown_source}** deleted. "
            f"Its assignments were moved to **{shown_target}**."
        )
