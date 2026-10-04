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
        f"Weight: {group.get('group_weight', 0)}%\n"
        f"Drop rules: {fence_untrusted_inline(str(group.get('rules') or {}), 'assignment group rules')}"
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

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    @validate_params
    async def get_assignment_group(
        course_identifier: str | int,
        assignment_group_id: str | int,
        include_assignments: bool = False,
    ) -> str:
        """Read one course assignment group and its drop rules without student data."""
        course_id = await get_course_id(course_identifier)
        params: dict[str, Any] = {"override_assignment_dates": False}
        if include_assignments:
            params["include[]"] = ["assignments"]
        response = await make_canvas_request(
            "get", canvas_path("courses", course_id, "assignment_groups", assignment_group_id),
            params=params,
        )
        if not isinstance(response, dict):
            return "Error: Canvas returned an invalid assignment group response."
        if "error" in response:
            return f"Error fetching assignment group: {response['error']}"
        return _format_group(response, include_assignments=include_assignments)

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
        drop_lowest: int | None = None,
        drop_highest: int | None = None,
        never_drop: list[int] | None = None,
    ) -> str:
        """Update group authoring settings and drop rules.

        Drop counts must be nonnegative. never_drop contains assignment IDs
        in this group; an empty list clears the exclusions. Omitted rule
        fields preserve their current values. Zero clears a drop count.
        """
        if name is not None and contains_fence_markers(name):
            return FENCE_LEAK_ERROR
        data: dict[str, Any] = {}
        if name is not None:
            data["name"] = name
        if position is not None:
            data["position"] = position
        if group_weight is not None:
            data["group_weight"] = group_weight
        rules: dict[str, Any] = {}
        for key, value in (("drop_lowest", drop_lowest), ("drop_highest", drop_highest)):
            if value is not None:
                if value < 0:
                    return f"Error: {key} must be nonnegative."
                rules[key] = value
        if never_drop is not None:
            if any(type(value) is not int or value < 1 for value in never_drop):
                return "Error: never_drop must contain positive assignment IDs."
            if len(set(never_drop)) != len(never_drop):
                return "Error: never_drop must not contain duplicate assignment IDs."
            rules["never_drop"] = never_drop
        if rules:
            data["rules"] = rules
        if not data:
            return "No assignment-group fields were provided to update."

        course_id = await get_course_id(course_identifier)
        if rules:
            current = await make_canvas_request(
                "get", canvas_path("courses", course_id, "assignment_groups", assignment_group_id),
                params={"include[]": ["assignments"], "override_assignment_dates": False},
            )
            if not isinstance(current, dict) or "error" in current:
                return "Error: could not verify the assignment group's current drop rules."
            assignments = current.get("assignments")
            if not isinstance(assignments, list):
                return "Error: Canvas did not return the group's assignment definitions."
            assignment_ids = {str(assignment.get("id")) for assignment in assignments}
            if never_drop is not None and any(str(value) not in assignment_ids for value in never_drop):
                return "Error: never_drop assignments must belong to this course assignment group."
            current_rules = current.get("rules") or {}
            if not isinstance(current_rules, dict):
                return "Error: Canvas returned invalid assignment-group rules."
            merged_rules = {
                key: current_rules[key]
                for key in ("drop_lowest", "drop_highest", "never_drop")
                if key in current_rules
            } | rules
            for key in ("drop_lowest", "drop_highest"):
                if key in merged_rules and (
                    type(merged_rules[key]) is not int or merged_rules[key] < 0
                ):
                    return "Error: Canvas returned invalid assignment-group drop counts."
            exclusions = merged_rules.get("never_drop", [])
            if not isinstance(exclusions, list) or any(
                type(value) not in (str, int) or not str(value).isdecimal()
                or int(value) < 1 for value in exclusions
            ):
                return "Error: Canvas returned invalid never-drop assignment IDs."
            data["rules"] = merged_rules
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
