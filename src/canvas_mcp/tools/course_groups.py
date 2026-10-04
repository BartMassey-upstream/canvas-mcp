"""Course-scoped collaborative groups with verified ownership and confirmations."""

from __future__ import annotations

import json
from typing import Any

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
from ..core.validation import coerce_canvas_id, validate_params
from ..core.write_confirmation import (
    ConfirmationGuard,
    preview_with_token,
    redeem_confirmation,
)

_GUARD = ConfirmationGuard(nothing_done="Nothing was changed.")


def _view(raw: dict[str, Any]) -> dict[str, Any]:
    result = {
        key: raw[key]
        for key in (
            "id",
            "course_id",
            "context_type",
            "group_category_id",
            "role",
            "self_signup",
            "auto_leader",
            "group_limit",
            "members_count",
            "join_level",
            "user_id",
            "group_id",
            "workflow_state",
            "moderator",
        )
        if key in raw
    }
    for key in ("name", "description"):
        if isinstance(raw.get(key), str):
            result[key] = fence_untrusted(raw[key], f"group {key}")
    return result


def _owned(raw: Any, course: str | int) -> bool:
    return (
        isinstance(raw, dict)
        and "error" not in raw
        and raw.get("context_type") == "Course"
        and str(raw.get("course_id")) == str(course)
        and raw.get("id") is not None
    )


def _editable(raw: dict[str, Any]) -> None:
    if (
        raw.get("role")
        or raw.get("sis_group_id")
        or raw.get("sis_group_category_id")
        or raw.get("sis_import_id")
        or raw.get("non_collaborative")
        or raw.get("progress")
    ):
        raise ValueError(
            "Only ordinary collaborative course groups are supported; known SIS-managed or special groups are refused."
        )


async def _course(identifier: str | int, permission: str | None = None) -> str:
    resolved = await get_course_id(identifier)
    if not str(resolved).isdigit():
        raw = await make_canvas_request("get", canvas_path("courses", resolved))
        if not isinstance(raw, dict) or "error" in raw or not raw.get("id"):
            raise ValueError("Could not resolve the course to a Canvas ID.")
        resolved = raw["id"]
    if permission:
        raw = await make_canvas_request(
            "get",
            canvas_path("courses", resolved, "permissions"),
            params={"permissions[]": [permission]},
        )
        if not isinstance(raw, dict) or raw.get(permission) is not True:
            raise ValueError(
                f"Canvas did not confirm {permission} permission in this course."
            )
    return str(resolved)


async def _category(
    course: str, category_id: str | int, *, edit: bool = False
) -> dict[str, Any]:
    raw = await make_canvas_request("get", canvas_path("group_categories", category_id))
    if not _owned(raw, course) or str(raw["id"]) != str(category_id):
        raise ValueError("Could not verify this group category belongs to the course.")
    if edit:
        _editable(raw)
    return dict(raw)


async def _group(
    course: str, group_id: str | int, *, edit: bool = False
) -> dict[str, Any]:
    raw = await make_canvas_request("get", canvas_path("groups", group_id))
    if not _owned(raw, course) or str(raw["id"]) != str(group_id):
        raise ValueError("Could not verify this group belongs to the course.")
    if edit:
        _editable(raw)
        if not raw.get("group_category_id"):
            raise ValueError("A normal course group category is required.")
        await _category(course, raw["group_category_id"], edit=True)
    return dict(raw)


async def _listing(
    path: str, params: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    raw = await fetch_all_paginated_results(path, {"per_page": 100, **(params or {})})
    if not isinstance(raw, list) or any(not isinstance(item, dict) for item in raw):
        raise ValueError(
            "Canvas did not return a complete inventory; nothing was changed."
        )
    return raw


def _text(**values: str | None) -> dict[str, Any]:
    payload = {key: value for key, value in values.items() if value is not None}
    if any(contains_fence_markers(value) for value in payload.values()):
        raise ValueError(FENCE_LEAK_ERROR)
    if "name" in payload and not payload["name"].strip():
        raise ValueError("name must not be blank")
    return payload


async def _change(
    name: str,
    course: str,
    method: str,
    path: str,
    payload: dict[str, Any],
    snapshot: Any,
    token: str | None,
    *,
    read_path: str | None = None,
    deleted_id: str | int | None = None,
    deleted_user_id: int | None = None,
) -> dict[str, Any] | str:
    fingerprint = _GUARD.fingerprint(
        name,
        course,
        method,
        path,
        json.dumps(payload, sort_keys=True),
        json.dumps(snapshot, sort_keys=True),
    )
    if not token:
        preview = fence_untrusted(
            json.dumps(
                {"course_id": course, "current": snapshot, "requested": payload},
                sort_keys=True,
            ),
            "group change preview",
        )
        return preview_with_token(
            _GUARD, fingerprint, name, preview, action="apply this change"
        )
    error = redeem_confirmation(_GUARD, token, fingerprint)
    if error:
        return error
    raw = await make_canvas_request(
        method,
        path,
        data=payload if payload else None,
        params=(
            {"override_sis_stickiness": "false"}
            if name == "update_course_group"
            else None
        ),
    )
    if not isinstance(raw, dict) or "error" in raw:
        return {
            "error": "Canvas did not confirm the group change. Check Canvas before retrying."
        }
    if method == "delete":
        if name == "remove_course_group_member":
            if raw.get("ok") is not True:
                return {
                    "error": "Membership deletion was not acknowledged. Check Canvas before retrying.",
                    "write_unconfirmed": True,
                }
            remaining = await fetch_all_paginated_results(
                path.rsplit("/", 1)[0], {"per_page": 100}
            )
            if (
                not isinstance(remaining, list)
                or any(
                    not isinstance(row, dict)
                    or not row.get("id")
                    or str(row.get("group_id")) != path.split("/")[2]
                    or not row.get("user_id")
                    for row in remaining
                )
                or any(
                    str(row.get("id")) == str(deleted_id)
                    or str(row.get("user_id")) == str(deleted_user_id)
                    for row in remaining
                )
            ):
                return {
                    "error": "Membership removal was not verified. Check Canvas before retrying.",
                    "write_unconfirmed": True,
                }
            return {"status": "deleted", "id": deleted_id}
        if str(raw.get("id")) != str(deleted_id):
            return {
                "error": "Deletion was not confirmed. Check Canvas before retrying.",
                "write_unconfirmed": True,
            }
        return {"status": "deleted", "id": deleted_id}
    if not raw.get("id"):
        return {
            "error": "The write response had no object ID. Check Canvas before retrying.",
            "write_unconfirmed": True,
        }
    if name == "add_course_group_member":
        membership_id = coerce_canvas_id(raw.get("id", ""))
        if membership_id is None or int(membership_id) <= 0:
            return {"error": "The membership ID was not confirmed. Check Canvas before retrying.", "write_unconfirmed": True}
        try:
            persisted = await make_canvas_request("get", path + canvas_path(membership_id))
        except Exception:
            return {"error": "Membership readback failed. Check Canvas before retrying.", "write_unconfirmed": True}
        if (
            not isinstance(persisted, dict) or "error" in persisted
            or coerce_canvas_id(persisted.get("id", "")) != membership_id
            or str(persisted.get("group_id")) != path.split("/")[2]
            or str(persisted.get("user_id")) != str(payload["user_id"])
            or not isinstance(persisted.get("workflow_state"), str)
            or persisted.get("workflow_state") not in {"accepted", "invited", "requested"}
        ):
            return {"error": "The membership's persisted identity and state were not confirmed. Check Canvas before retrying.", "write_unconfirmed": True}
        membership_state = persisted["workflow_state"]
        return {"status": "created" if membership_state == "accepted" else "pending",
                "membership_state": membership_state,
                "membership_active": membership_state == "accepted",
                "result": _view(persisted)}
    if read_path:
        raw = await make_canvas_request("get", read_path)
    if (
        not isinstance(raw, dict)
        or "error" in raw
        or any(raw.get(key) != value for key, value in payload.items())
    ):
        return {
            "error": "The requested group fields were not confirmed. Check Canvas before retrying.",
            "write_unconfirmed": True,
        }
    membership = "/memberships" in path
    if membership and str(raw.get("group_id")) != path.split("/")[2]:
        return {
            "error": "The resulting group membership was not confirmed.",
            "write_unconfirmed": True,
        }
    if method == "put" and str(raw.get("id")) != path.rsplit("/", 1)[-1]:
        return {
            "error": "The updated object ID was not confirmed.",
            "write_unconfirmed": True,
        }
    if not membership and not _owned(raw, course):
        return {
            "error": "The resulting course ownership was not confirmed.",
            "write_unconfirmed": True,
        }
    return {"status": "updated" if method == "put" else "created", "result": _view(raw)}


async def _safe_membership(
    course: str, group: dict[str, Any], user_id: int
) -> dict[str, Any]:
    if isinstance(user_id, bool) or user_id <= 0:
        raise ValueError("user_id must be a positive Canvas user ID")
    assignments = await _listing(canvas_path("courses", course, "assignments"))
    linked = [
        item
        for item in assignments
        if str(item.get("group_category_id")) == str(group["group_category_id"])
    ]
    if any(item.get("has_submitted_submissions") is not False for item in linked):
        raise ValueError(
            "Group membership changes are refused when linked assignments have student work or unknown submission status."
        )
    enrollments = await _listing(
        canvas_path("courses", course, "enrollments"),
        {"user_id": user_id, "type[]": ["StudentEnrollment"], "state[]": ["active"]},
    )
    if not any(
        str(item.get("user_id")) == str(user_id)
        and item.get("type") == "StudentEnrollment"
        and item.get("enrollment_state") == "active"
        for item in enrollments
    ):
        raise ValueError(
            "An active student enrollment in this course could not be verified."
        )
    groups = await _listing(
        canvas_path("group_categories", group["group_category_id"], "groups")
    )
    memberships: list[dict[str, Any]] = []
    for sibling in groups:
        if not _owned(sibling, course) or str(sibling.get("group_category_id")) != str(
            group["group_category_id"]
        ):
            raise ValueError("Could not verify every group in this category.")
        members = await _listing(canvas_path("groups", sibling["id"], "memberships"))
        memberships.extend(
            _view(item) for item in members if str(item.get("user_id")) == str(user_id)
        )
    return {
        "group": _view(group),
        "memberships": memberships,
        "linked_assignments": [
            {
                "id": item.get("id"),
                "has_submitted_submissions": item.get("has_submitted_submissions"),
            }
            for item in linked
        ],
    }


def register_course_group_tools(mcp: FastMCP) -> None:
    """Register educator-only group definitions and guarded membership management."""

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    @validate_params
    async def list_group_categories(course_identifier: str | int) -> dict[str, Any]:
        """List collaborative group sets owned by this course; no member records."""
        try:
            course = await _course(course_identifier)
            rows = await _listing(canvas_path("courses", course, "group_categories"))
            return {
                "group_categories": [_view(row) for row in rows if _owned(row, course)]
            }
        except ValueError as exc:
            return {"error": str(exc)}

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    @validate_params
    async def get_course_group(
        course_identifier: str | int, group_id: str | int
    ) -> dict[str, Any]:
        """Read one group after verifying it belongs to the requested course."""
        try:
            return {
                "group": _view(await _group(await _course(course_identifier), group_id))
            }
        except ValueError as exc:
            return {"error": str(exc)}

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    @validate_params
    async def list_course_group_memberships(
        course_identifier: str | int, group_id: str | int
    ) -> dict[str, Any]:
        """Read course group membership IDs, user IDs, state and moderator flags."""
        try:
            await _group(await _course(course_identifier), group_id)
            return {
                "memberships": [
                    _view(row)
                    for row in await _listing(
                        canvas_path("groups", group_id, "memberships")
                    )
                ]
            }
        except ValueError as exc:
            return {"error": str(exc)}

    @mcp.tool(annotations=ToolAnnotations(destructive_hint=False, idempotent_hint=False))
    @validate_params
    async def create_group_category(
        course_identifier: str | int, name: str, confirmation_token: str | None = None
    ) -> dict[str, Any] | str:
        """Preview and create a course group set with self-signup disabled."""
        try:
            payload = _text(name=name)
            course = await _course(course_identifier, "manage_groups_add")
            return await _change(
                "create_group_category",
                course,
                "post",
                canvas_path("courses", course, "group_categories"),
                payload,
                {},
                confirmation_token,
            )
        except ValueError as exc:
            return {"error": str(exc)}

    @mcp.tool(annotations=ToolAnnotations(destructive_hint=True, idempotent_hint=True))
    @validate_params
    async def update_group_category(
        course_identifier: str | int,
        group_category_id: str | int,
        name: str | None = None,
        self_signup: str | None = None,
        auto_leader: str | None = None,
        group_limit: int | None = None,
        confirmation_token: str | None = None,
    ) -> dict[str, Any] | str:
        """Preview group-set settings; use 'disabled' to clear signup or auto-leader.

        group_limit=0 removes the limit. Omitted settings remain unchanged.
        """
        try:
            payload = _text(name=name)
            for key, value, choices in (
                ("self_signup", self_signup, {"enabled", "restricted", "disabled"}),
                ("auto_leader", auto_leader, {"first", "random", "disabled"}),
            ):
                if value is not None:
                    if value not in choices:
                        raise ValueError(f"Invalid {key}")
                    payload[key] = None if value == "disabled" else value
            if group_limit is not None:
                if isinstance(group_limit, bool) or group_limit < 0:
                    raise ValueError("group_limit must be a nonnegative integer")
                payload["group_limit"] = group_limit or None
            if not payload:
                raise ValueError("Supply at least one setting")
            course = await _course(course_identifier, "manage_groups_manage")
            current = await _category(course, group_category_id, edit=True)
            if payload.get("group_limit") and not payload.get(
                "self_signup", current.get("self_signup")
            ):
                raise ValueError("A group limit requires self signup")
            path = canvas_path("group_categories", group_category_id)
            return await _change(
                "update_group_category",
                course,
                "put",
                path,
                payload,
                _view(current),
                confirmation_token,
                read_path=path,
            )
        except ValueError as exc:
            return {"error": str(exc)}

    @mcp.tool(annotations=ToolAnnotations(destructive_hint=True, idempotent_hint=True))
    @validate_params
    async def delete_group_category(
        course_identifier: str | int,
        group_category_id: str | int,
        confirmation_token: str | None = None,
    ) -> dict[str, Any] | str:
        """Preview deletion of an empty course group set; never cascade to groups."""
        try:
            course = await _course(course_identifier, "manage_groups_delete")
            current = await _category(course, group_category_id, edit=True)
            if await _listing(
                canvas_path("group_categories", group_category_id, "groups")
            ):
                raise ValueError("Only empty group categories can be deleted")
            assignments = await _listing(canvas_path("courses", course, "assignments"))
            if any(
                str(row.get("group_category_id")) == str(group_category_id)
                for row in assignments
            ):
                raise ValueError("This group category is still linked to assignments")
            return await _change(
                "delete_group_category",
                course,
                "delete",
                canvas_path("group_categories", group_category_id),
                {},
                _view(current),
                confirmation_token,
                deleted_id=group_category_id,
            )
        except ValueError as exc:
            return {"error": str(exc)}

    @mcp.tool(annotations=ToolAnnotations(destructive_hint=False, idempotent_hint=False))
    @validate_params
    async def create_course_group(
        course_identifier: str | int,
        group_category_id: str | int,
        name: str,
        description: str | None = None,
        confirmation_token: str | None = None,
    ) -> dict[str, Any] | str:
        """Preview and create an invitation-only collaborative course group."""
        try:
            payload = {
                **_text(name=name, description=description),
                "join_level": "invitation_only",
            }
            course = await _course(course_identifier, "manage_groups_add")
            category = await _category(course, group_category_id, edit=True)
            return await _change(
                "create_course_group",
                course,
                "post",
                canvas_path("group_categories", group_category_id, "groups"),
                payload,
                _view(category),
                confirmation_token,
            )
        except ValueError as exc:
            return {"error": str(exc)}

    @mcp.tool(annotations=ToolAnnotations(destructive_hint=True, idempotent_hint=True))
    @validate_params
    async def update_course_group(
        course_identifier: str | int,
        group_id: str | int,
        name: str | None = None,
        description: str | None = None,
        confirmation_token: str | None = None,
    ) -> dict[str, Any] | str:
        """Preview and edit a course group's name and description, preserving members.

        Known SIS-managed groups are refused. Hidden SIS provenance cannot be
        certified; Canvas's documented SIS stickiness override is disabled.
        """
        try:
            payload = _text(name=name, description=description)
            if not payload:
                raise ValueError("Supply a name or description")
            course = await _course(course_identifier, "manage_groups_manage")
            current = await _group(course, group_id, edit=True)
            path = canvas_path("groups", group_id)
            return await _change(
                "update_course_group",
                course,
                "put",
                path,
                payload,
                _view(current),
                confirmation_token,
                read_path=path,
            )
        except ValueError as exc:
            return {"error": str(exc)}

    @mcp.tool(annotations=ToolAnnotations(destructive_hint=True, idempotent_hint=True))
    @validate_params
    async def delete_course_group(
        course_identifier: str | int,
        group_id: str | int,
        confirmation_token: str | None = None,
    ) -> dict[str, Any] | str:
        """Preview deletion only of a group with no members, content or assignments."""
        try:
            course = await _course(course_identifier, "manage_groups_delete")
            current = await _group(course, group_id, edit=True)
            for family in (
                "memberships",
                "files",
                "pages",
                "discussion_topics",
                "collaborations",
            ):
                params = (
                    {"include_announcements": True}
                    if family == "discussion_topics"
                    else None
                )
                if await _listing(canvas_path("groups", group_id, family), params):
                    raise ValueError(
                        "Only groups without members or content can be deleted"
                    )
            if await _listing(
                canvas_path("calendar_events"),
                {
                    "type": "event",
                    "all_events": True,
                    "context_codes[]": [f"group_{current['id']}"],
                },
            ):
                raise ValueError("Only groups without calendar content can be deleted")
            assignments = await _listing(canvas_path("courses", course, "assignments"))
            if any(
                str(row.get("group_category_id")) == str(current["group_category_id"])
                for row in assignments
            ):
                raise ValueError("The group category is still linked to assignments")
            return await _change(
                "delete_course_group",
                course,
                "delete",
                canvas_path("groups", group_id),
                {},
                _view(current),
                confirmation_token,
                deleted_id=group_id,
            )
        except ValueError as exc:
            return {"error": str(exc)}

    @mcp.tool(annotations=ToolAnnotations(destructive_hint=True, idempotent_hint=True))
    @validate_params
    async def add_course_group_member(
        course_identifier: str | int,
        group_id: str | int,
        user_id: int,
        confirmation_token: str | None = None,
    ) -> dict[str, Any] | str:
        """Preview adding an active course student; refuse implicit moves or existing work."""
        try:
            course = await _course(course_identifier, "manage_groups_manage")
            current = await _group(course, group_id, edit=True)
            snapshot = await _safe_membership(course, current, user_id)
            if snapshot["memberships"]:
                raise ValueError(
                    "The student already has a membership in this group set; explicitly remove it first."
                )
            return await _change(
                "add_course_group_member",
                course,
                "post",
                canvas_path("groups", group_id, "memberships"),
                {"user_id": user_id},
                snapshot,
                confirmation_token,
            )
        except ValueError as exc:
            return {"error": str(exc)}

    @mcp.tool(annotations=ToolAnnotations(destructive_hint=True, idempotent_hint=True))
    @validate_params
    async def remove_course_group_member(
        course_identifier: str | int,
        group_id: str | int,
        user_id: int,
        confirmation_token: str | None = None,
    ) -> dict[str, Any] | str:
        """Preview removing one course group member; refuse groups with submitted work."""
        try:
            course = await _course(course_identifier, "manage_groups_manage")
            current = await _group(course, group_id, edit=True)
            snapshot = await _safe_membership(course, current, user_id)
            members = [
                row
                for row in snapshot["memberships"]
                if str(row.get("group_id")) == str(group_id)
            ]
            if len(members) != 1 or not members[0].get("id"):
                raise ValueError("The exact group membership could not be verified")
            return await _change(
                "remove_course_group_member",
                course,
                "delete",
                canvas_path("groups", group_id, "memberships", members[0]["id"]),
                {},
                snapshot,
                confirmation_token,
                deleted_id=members[0]["id"],
                deleted_user_id=user_id,
            )
        except ValueError as exc:
            return {"error": str(exc)}
