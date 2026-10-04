"""Course-owned learning outcome definitions and organization tools."""

from __future__ import annotations

import json
from typing import Any, TypeGuard

from fastmcp import FastMCP
from mcp.types import ToolAnnotations

from ..core.cache import get_course_id
from ..core.client import fetch_all_paginated_results, make_canvas_request
from ..core.path import canvas_path
from ..core.untrusted_content import (
    FENCE_LEAK_ERROR,
    contains_fence_markers,
    fence_untrusted,
)
from ..core.validation import validate_params
from ..core.write_confirmation import (
    ConfirmationGuard,
    preview_with_token,
    redeem_confirmation,
)

_OUTCOME_DELETE_GUARD = ConfirmationGuard(nothing_done="Nothing was deleted.")
_METHODS = {
    "weighted_average",
    "decaying_average",
    "n_mastery",
    "latest",
    "highest",
    "average",
}


def _snapshot(raw: dict[str, Any], *, group: bool = False) -> dict[str, Any]:
    fields: tuple[str, ...] = ("id", "context_id", "context_type", "can_edit")
    if not group:
        fields += (
            "points_possible",
            "mastery_points",
            "calculation_method",
            "calculation_int",
        )
    result = {field: raw[field] for field in fields if field in raw}
    for field in ("title", "display_name", "description", "vendor_guid"):
        if isinstance(raw.get(field), str):
            result[field] = fence_untrusted(
                raw[field], f"outcome {'group ' if group else ''}{field}"
            )
    if group and isinstance(raw.get("parent_outcome_group"), dict):
        result["parent_outcome_group_id"] = raw["parent_outcome_group"].get("id")
    if not group and isinstance(raw.get("ratings"), list):
        result["ratings"] = [
            {
                "points": rating.get("points"),
                "description": fence_untrusted(
                    str(rating.get("description", "")), "outcome rating description"
                ),
            }
            for rating in raw["ratings"]
            if isinstance(rating, dict)
        ]
    return result


def _owned(raw: object, course_id: str | int) -> TypeGuard[dict[str, Any]]:
    return (
        isinstance(raw, dict)
        and raw.get("context_type") == "Course"
        and str(raw.get("context_id")) == str(course_id)
    )


def _text_payload(**fields: str | None) -> dict[str, Any]:
    result = {key: value for key, value in fields.items() if value is not None}
    if any(contains_fence_markers(value) for value in result.values()):
        raise ValueError(FENCE_LEAK_ERROR)
    if "title" in result and not result["title"].strip():
        raise ValueError("title must not be empty")
    return result


async def _course_group(
    course_id: str | int, group_id: str | int | None
) -> dict[str, Any]:
    if group_id is None:
        groups = await fetch_all_paginated_results(
            canvas_path("courses", course_id, "outcome_groups"), {"per_page": 100}
        )
        if not isinstance(groups, list):
            return {"error": "Could not discover the course root outcome group."}
        roots = [
            group
            for group in groups
            if _owned(group, course_id) and group.get("parent_outcome_group") is None
        ]
        if len(roots) != 1:
            return {"error": "Could not uniquely verify the course root outcome group."}
        raw = roots[0]
    else:
        raw = await make_canvas_request(
            "get", canvas_path("courses", course_id, "outcome_groups", group_id)
        )
    if (
        not _owned(raw, course_id)
        or "error" in raw
        or not raw.get("id")
        or (group_id is not None and str(raw["id"]) != str(group_id))
    ):
        return {"error": "Could not verify a course-owned outcome group."}
    return raw


async def _course_outcome(
    course_id: str | int, outcome_id: str | int
) -> dict[str, Any]:
    links = await fetch_all_paginated_results(
        canvas_path("courses", course_id, "outcome_group_links"), {"per_page": 100}
    )
    if not isinstance(links, list) or not any(
        isinstance(link, dict)
        and isinstance(link.get("outcome"), dict)
        and str(link["outcome"].get("id")) == str(outcome_id)
        for link in links
    ):
        return {"error": "The outcome is not confirmed linked in this course."}
    raw = await make_canvas_request("get", canvas_path("outcomes", outcome_id))
    if (
        not _owned(raw, course_id)
        or "error" in raw
        or str(raw.get("id")) != str(outcome_id)
    ):
        return {"error": "Only outcomes owned by this course are supported."}
    return raw


def _write_result(
    raw: object,
    course_id: str | int,
    *,
    group: bool = False,
    expected_id: str | int | None = None,
    expected_fields: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if isinstance(raw, dict) and "error" in raw:
        return {"error": raw["error"]}
    if (
        not _owned(raw, course_id)
        or not raw.get("id")
        or (expected_id is not None and str(raw["id"]) != str(expected_id))
    ):
        return {
            "error": "Canvas accepted the request but the expected course-owned object was not confirmed.",
            "write_unconfirmed": True,
        }
    if expected_fields and any(
        raw.get(key) != value for key, value in expected_fields.items()
    ):
        return {
            "error": "Canvas accepted the request but the requested field values were not confirmed.",
            "write_unconfirmed": True,
        }
    return {
        "status": "updated" if expected_id is not None else "created",
        "group" if group else "outcome": _snapshot(raw, group=group),
    }


def _link_matches(
    raw: object, course_id: str | int, group_id: str | int
) -> TypeGuard[dict[str, Any]]:
    return (
        _owned(raw, course_id)
        and isinstance(raw.get("outcome"), dict)
        and isinstance(raw.get("outcome_group"), dict)
        and str(raw["outcome_group"].get("id")) == str(group_id)
    )


def register_outcome_tools(mcp: FastMCP) -> None:
    """Register course creator/educator definition tools without outcome results."""

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    @validate_params
    async def list_outcome_groups(course_identifier: str | int) -> dict[str, Any]:
        """List this course's outcome groups, excluding account/global groups."""
        course_id = await get_course_id(course_identifier)
        raw = await fetch_all_paginated_results(
            canvas_path("courses", course_id, "outcome_groups"), {"per_page": 100}
        )
        if not isinstance(raw, list):
            return {"error": "Could not list course outcome groups."}
        return {
            "groups": [
                _snapshot(group, group=True)
                for group in raw
                if _owned(group, course_id)
            ]
        }

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    @validate_params
    async def get_outcome_group(
        course_identifier: str | int, group_id: str | int | None = None
    ) -> dict[str, Any]:
        """Read a course outcome group; omit group_id to read the course root."""
        course_id = await get_course_id(course_identifier)
        raw = await _course_group(course_id, group_id)
        return raw if "error" in raw else {"group": _snapshot(raw, group=True)}

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    @validate_params
    async def list_course_outcomes(
        course_identifier: str | int, group_id: str | int | None = None
    ) -> dict[str, Any]:
        """List course-owned outcome definitions and group links without assessment flags."""
        course_id = await get_course_id(course_identifier)
        if group_id is None:
            endpoint = canvas_path("courses", course_id, "outcome_group_links")
        else:
            group = await _course_group(course_id, group_id)
            if "error" in group:
                return group
            endpoint = canvas_path(
                "courses", course_id, "outcome_groups", group_id, "outcomes"
            )
        raw = await fetch_all_paginated_results(
            endpoint, {"per_page": 100, "outcome_style": "full"}
        )
        if not isinstance(raw, list):
            return {"error": "Could not list course outcomes."}
        return {
            "links": [
                {
                    "outcome": _snapshot(link["outcome"]),
                    "group_id": (link.get("outcome_group") or {}).get("id"),
                }
                for link in raw
                if isinstance(link, dict) and _owned(link.get("outcome"), course_id)
            ]
        }

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    @validate_params
    async def get_course_outcome(
        course_identifier: str | int, outcome_id: str | int
    ) -> dict[str, Any]:
        """Read one course-owned outcome definition after verifying course linkage."""
        course_id = await get_course_id(course_identifier)
        raw = await _course_outcome(course_id, outcome_id)
        return raw if "error" in raw else {"outcome": _snapshot(raw)}

    @mcp.tool(
        annotations=ToolAnnotations(destructive_hint=False, idempotent_hint=False)
    )
    @validate_params
    async def create_outcome_group(
        course_identifier: str | int,
        title: str,
        parent_group_id: str | int | None = None,
        description: str | None = None,
        vendor_guid: str | None = None,
    ) -> dict[str, Any]:
        """Create an empty group below a course-owned parent, or the course root."""
        try:
            data = _text_payload(
                title=title, description=description, vendor_guid=vendor_guid
            )
        except ValueError as exc:
            return {"error": str(exc)}
        course_id = await get_course_id(course_identifier)
        parent = await _course_group(course_id, parent_group_id)
        if "error" in parent:
            return parent
        raw = await make_canvas_request(
            "post",
            canvas_path(
                "courses", course_id, "outcome_groups", parent["id"], "subgroups"
            ),
            data=data,
        )
        return _write_result(raw, course_id, group=True, expected_fields=data)

    @mcp.tool(annotations=ToolAnnotations(destructive_hint=True, idempotent_hint=True))
    @validate_params
    async def update_outcome_group(
        course_identifier: str | int,
        group_id: str | int,
        title: str | None = None,
        description: str | None = None,
        vendor_guid: str | None = None,
    ) -> dict[str, Any]:
        """Edit course-owned group text; structural moves are excluded."""
        try:
            data = _text_payload(
                title=title, description=description, vendor_guid=vendor_guid
            )
        except ValueError as exc:
            return {"error": str(exc)}
        if not data:
            return {"error": "No changes specified."}
        course_id = await get_course_id(course_identifier)
        group = await _course_group(course_id, group_id)
        if "error" in group:
            return group
        raw = await make_canvas_request(
            "put",
            canvas_path("courses", course_id, "outcome_groups", group_id),
            data=data,
        )
        return _write_result(
            raw, course_id, group=True, expected_id=group_id, expected_fields=data
        )

    @mcp.tool(annotations=ToolAnnotations(destructive_hint=True, idempotent_hint=True))
    @validate_params
    async def delete_outcome_group(
        course_identifier: str | int,
        group_id: str | int,
        confirmation_token: str | None = None,
    ) -> str:
        """Preview and confirm deletion of an empty non-root course-owned group."""
        course_id = await get_course_id(course_identifier)
        group = await _course_group(course_id, group_id)
        if "error" in group:
            return str(group["error"])
        root = await _course_group(course_id, None)
        if "error" in root or str(root.get("id")) == str(group_id):
            return "The course root outcome group cannot be deleted."
        base = canvas_path("courses", course_id, "outcome_groups", group_id)
        for child in ("subgroups", "outcomes"):
            children = await fetch_all_paginated_results(
                canvas_path("courses", course_id, "outcome_groups", group_id, child),
                {"per_page": 100},
            )
            if not isinstance(children, list) or children:
                return "Only confirmed empty outcome groups can be deleted."
        fingerprint = _OUTCOME_DELETE_GUARD.fingerprint(
            "delete_outcome_group",
            str(course_id),
            str(group_id),
            json.dumps(group, sort_keys=True),
        )
        if not confirmation_token:
            return preview_with_token(
                _OUTCOME_DELETE_GUARD,
                fingerprint,
                "delete_outcome_group",
                f"Would delete empty outcome group {group_id}: {fence_untrusted(str(group.get('title', '')), 'outcome group title')}",
            )
        error = redeem_confirmation(
            _OUTCOME_DELETE_GUARD, confirmation_token, fingerprint
        )
        if error:
            return error
        raw = await make_canvas_request("delete", base)
        if (
            not isinstance(raw, dict)
            or "error" in raw
            or str(raw.get("id")) != str(group_id)
        ):
            return "Could not confirm deletion of the outcome group. Verify in Canvas before retrying."
        return f"Deleted empty outcome group {group_id}."

    @mcp.tool(
        annotations=ToolAnnotations(destructive_hint=False, idempotent_hint=False)
    )
    @validate_params
    async def create_course_outcome(
        course_identifier: str | int,
        title: str,
        group_id: str | int | None = None,
        display_name: str | None = None,
        description: str | None = None,
        vendor_guid: str | None = None,
        ratings_json: str | None = None,
        mastery_points: int | None = None,
        calculation_method: str | None = None,
        calculation_int: int | None = None,
    ) -> dict[str, Any]:
        """Create and link a course-owned outcome, optionally with a rating scale.

        ratings_json must be a nonempty array of description/integer points objects.
        """
        try:
            data = _text_payload(
                title=title,
                display_name=display_name,
                description=description,
                vendor_guid=vendor_guid,
            )
            if calculation_method is not None:
                if calculation_method not in _METHODS:
                    raise ValueError("Unsupported calculation_method.")
                data["calculation_method"] = calculation_method
            if calculation_int is not None:
                if (
                    calculation_method
                    not in {"weighted_average", "decaying_average", "n_mastery"}
                    or calculation_int <= 0
                ):
                    raise ValueError(
                        "calculation_int requires a compatible calculation method and a positive value."
                    )
                data["calculation_int"] = calculation_int
            if ratings_json is not None:
                ratings = json.loads(ratings_json)
                if not isinstance(ratings, list) or not ratings:
                    raise ValueError("ratings_json must be a nonempty array.")
                for rating in ratings:
                    if (
                        not isinstance(rating, dict)
                        or set(rating) != {"description", "points"}
                        or not isinstance(rating["description"], str)
                        or type(rating["points"]) is not int
                        or rating["points"] < 0
                    ):
                        raise ValueError(
                            "Each rating needs a description and nonnegative integer points."
                        )
                    _text_payload(description=rating["description"])
                data["ratings"] = ratings
            if mastery_points is not None:
                if (
                    ratings_json is None
                    or mastery_points < 0
                    or mastery_points > max(r["points"] for r in data["ratings"])
                ):
                    raise ValueError(
                        "mastery_points requires ratings and must be within their point range."
                    )
                data["mastery_points"] = mastery_points
        except (ValueError, TypeError) as exc:
            return {"error": str(exc)}
        course_id = await get_course_id(course_identifier)
        group = await _course_group(course_id, group_id)
        if "error" in group:
            return group
        raw = await make_canvas_request(
            "post",
            canvas_path(
                "courses", course_id, "outcome_groups", group["id"], "outcomes"
            ),
            data=data,
        )
        if isinstance(raw, dict) and "error" in raw:
            return {"error": raw["error"]}
        if not _link_matches(raw, course_id, group["id"]):
            return {
                "error": "Canvas accepted the request but the expected course outcome link was not confirmed.",
                "write_unconfirmed": True,
            }
        created = raw["outcome"]
        if _owned(created, course_id) and created.get("id"):
            created = await make_canvas_request(
                "get", canvas_path("outcomes", created["id"])
            )
        return _write_result(created, course_id, expected_fields=data)

    @mcp.tool(annotations=ToolAnnotations(destructive_hint=True, idempotent_hint=True))
    @validate_params
    async def update_course_outcome(
        course_identifier: str | int,
        outcome_id: str | int,
        title: str | None = None,
        display_name: str | None = None,
        description: str | None = None,
        vendor_guid: str | None = None,
    ) -> dict[str, Any]:
        """Update unassessed course-owned outcome text; rating/calculation replacement is excluded."""
        try:
            data = _text_payload(
                title=title,
                display_name=display_name,
                description=description,
                vendor_guid=vendor_guid,
            )
        except ValueError as exc:
            return {"error": str(exc)}
        if not data:
            return {"error": "No changes specified."}
        course_id = await get_course_id(course_identifier)
        outcome = await _course_outcome(course_id, outcome_id)
        if "error" in outcome:
            return outcome
        if (
            outcome.get("assessed") is not False
            or outcome.get("has_updateable_rubrics") is not False
        ):
            return {
                "error": "Outcome updates require confirmed absence of assessments and propagating rubric changes."
            }
        raw = await make_canvas_request(
            "put", canvas_path("outcomes", outcome_id), data=data
        )
        return _write_result(
            raw, course_id, expected_id=outcome_id, expected_fields=data
        )

    @mcp.tool(annotations=ToolAnnotations(destructive_hint=False, idempotent_hint=True))
    @validate_params
    async def link_course_outcome(
        course_identifier: str | int, group_id: str | int, outcome_id: str | int
    ) -> dict[str, Any]:
        """Link an already course-owned outcome into another group in this course."""
        course_id = await get_course_id(course_identifier)
        group = await _course_group(course_id, group_id)
        if "error" in group:
            return group
        outcome = await _course_outcome(course_id, outcome_id)
        if "error" in outcome:
            return outcome
        raw = await make_canvas_request(
            "put",
            canvas_path(
                "courses", course_id, "outcome_groups", group_id, "outcomes", outcome_id
            ),
            data={},
        )
        if isinstance(raw, dict) and "error" in raw:
            return {"error": raw["error"]}
        if not _link_matches(raw, course_id, group_id):
            return {
                "error": "Canvas accepted the request but the expected course outcome link was not confirmed.",
                "write_unconfirmed": True,
            }
        result = _write_result(
            raw.get("outcome") if isinstance(raw, dict) else None,
            course_id,
            expected_id=outcome_id,
        )
        if "error" not in result:
            result["status"] = "linked"
            result["group_id"] = group_id
        return result

    @mcp.tool(annotations=ToolAnnotations(destructive_hint=True, idempotent_hint=True))
    @validate_params
    async def unlink_course_outcome(
        course_identifier: str | int,
        group_id: str | int,
        outcome_id: str | int,
        confirmation_token: str | None = None,
    ) -> str:
        """Preview and confirm unlinking an unassessed course-owned outcome.

        Canvas also deletes the outcome if this removes its final link anywhere.
        """
        course_id = await get_course_id(course_identifier)
        group = await _course_group(course_id, group_id)
        if "error" in group:
            return str(group["error"])
        outcome = await _course_outcome(course_id, outcome_id)
        if "error" in outcome:
            return str(outcome["error"])
        links = await fetch_all_paginated_results(
            canvas_path("courses", course_id, "outcome_groups", group_id, "outcomes"),
            {"per_page": 100},
        )
        if not isinstance(links, list):
            return "Could not verify the outcome link."
        matches = [
            link
            for link in links
            if isinstance(link, dict)
            and isinstance(link.get("outcome"), dict)
            and str(link["outcome"].get("id")) == str(outcome_id)
        ]
        if (
            len(matches) != 1
            or matches[0].get("can_unlink") is not True
            or matches[0].get("assessed") is not False
            or outcome.get("assessed") is not False
        ):
            return "Only confirmed unassessed, unlinkable outcomes can be unlinked."
        fingerprint = _OUTCOME_DELETE_GUARD.fingerprint(
            "unlink_course_outcome",
            str(course_id),
            str(group_id),
            str(outcome_id),
            json.dumps([group, outcome, matches], sort_keys=True),
        )
        if not confirmation_token:
            return preview_with_token(
                _OUTCOME_DELETE_GUARD,
                fingerprint,
                "unlink_course_outcome",
                f"Would unlink outcome {outcome_id} from group {group_id}: {fence_untrusted(str(outcome.get('title', '')), 'outcome title')}. Canvas deletes the outcome definition if this was its final link in any context.",
            )
        error = redeem_confirmation(
            _OUTCOME_DELETE_GUARD, confirmation_token, fingerprint
        )
        if error:
            return error
        raw = await make_canvas_request(
            "delete",
            canvas_path(
                "courses", course_id, "outcome_groups", group_id, "outcomes", outcome_id
            ),
        )
        if (
            not _link_matches(raw, course_id, group_id)
            or "error" in raw
            or not isinstance(raw.get("outcome"), dict)
            or str(raw["outcome"].get("id")) != str(outcome_id)
        ):
            return "Could not confirm unlinking the outcome. Verify in Canvas before retrying."
        return f"Unlinked outcome {outcome_id} from group {group_id}; its final link may also have deleted the definition."
