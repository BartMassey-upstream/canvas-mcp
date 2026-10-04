"""Self-only planning, calendar, module progression, and submission history."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from typing import Any, TypeGuard
from urllib.parse import urlsplit

from fastmcp import FastMCP
from mcp.types import ToolAnnotations

from ..core.cache import get_course_id
from ..core.client import fetch_all_paginated_results, make_canvas_request
from ..core.config import get_config
from ..core.course_policy import (
    assert_no_identity_override,
    check_student_write_allowed,
)
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

_PLANNING_GUARD = ConfirmationGuard(nothing_done="Nothing was changed.")
_TYPES = {
    "assignment",
    "announcement",
    "discussion_topic",
    "wiki_page",
    "quiz",
    "planner_note",
    "calendar_event",
}
_TYPE_ALIASES = {
    "Assignment": "assignment",
    "Announcement": "announcement",
    "DiscussionTopic": "discussion_topic",
    "WikiPage": "wiki_page",
    "Quiz": "quiz",
    "PlannerNote": "planner_note",
    "CalendarEvent": "calendar_event",
}
_FIELDS = {
    "note": (
        "id",
        "course_id",
        "todo_date",
        "workflow_state",
        "linked_object_id",
        "linked_object_type",
    ),
    "override": (
        "id",
        "plannable_type",
        "plannable_id",
        "assignment_id",
        "workflow_state",
        "marked_complete",
        "dismissed",
    ),
    "bookmark": ("id", "position"),
    "calendar": (
        "id",
        "context_code",
        "start_at",
        "end_at",
        "all_day",
        "all_day_date",
        "workflow_state",
    ),
    "file": ("id", "size", "content-type", "locked", "hidden", "locked_for_user"),
    "submission": (
        "id",
        "assignment_id",
        "attempt",
        "submitted_at",
        "submission_type",
        "workflow_state",
        "score",
        "late",
        "missing",
        "excused",
    ),
    "module": (
        "id",
        "position",
        "state",
        "completed_at",
        "unlock_at",
        "require_sequential_progress",
        "prerequisite_module_ids",
        "items_count",
    ),
    "item": (
        "id",
        "module_id",
        "type",
        "content_id",
        "position",
        "indent",
        "published",
    ),
    "course": ("id", "workflow_state"),
}
_TEXT = {
    "note": ("title", "description", "details"),
    "bookmark": ("name", "url"),
    "calendar": ("title", "description", "location_name", "location_address"),
    "file": ("filename", "display_name"),
    "submission": ("body", "url", "grade"),
    "module": ("name",),
    "item": ("title", "page_url", "external_url"),
    "course": ("name", "course_code"),
}


def _planner_type(value: object) -> str:
    return _TYPE_ALIASES.get(value, value) if isinstance(value, str) else ""


def _record(raw: object) -> TypeGuard[dict[str, Any]]:
    return isinstance(raw, dict) and "error" not in raw


def _scalar_fields(raw: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    return {
        key: raw[key]
        for key in fields
        if key in raw
        and (raw[key] is None or isinstance(raw[key], (str, int, float, bool)))
    }


def _day(value: str, label: str) -> date:
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError(f"{label} must use YYYY-MM-DD.")
    return parsed


def _snapshot(raw: dict[str, Any], kind: str) -> dict[str, Any]:
    result = _scalar_fields(raw, _FIELDS[kind])
    if kind == "module" and isinstance(raw.get("prerequisite_module_ids"), list):
        result["prerequisite_module_ids"] = [
            value
            for value in raw["prerequisite_module_ids"]
            if isinstance(value, (str, int)) and coerce_canvas_id(value) is not None
        ]
    for key in _TEXT.get(kind, ()):
        if isinstance(raw.get(key), str):
            result[key] = fence_untrusted(raw[key], f"my {kind} {key}")
    if kind == "submission" and isinstance(raw.get("attachments"), list):
        result["attachments"] = [
            _snapshot(entry, "file")
            for entry in raw["attachments"]
            if isinstance(entry, dict)
        ]
    if kind == "item" and isinstance(raw.get("completion_requirement"), dict):
        result["completion_requirement"] = _scalar_fields(
            raw["completion_requirement"],
            ("type", "min_score", "min_percentage", "completed"),
        )
    return result


def _numeric(value: str | int, label: str) -> str:
    result = coerce_canvas_id(value)
    if result is None:
        raise ValueError(f"{label} must be a numeric Canvas ID.")
    return result


async def _self_id() -> str:
    raw = await make_canvas_request("get", canvas_path("users", "self", "profile"))
    if not _record(raw):
        raise ValueError("Could not verify your Canvas identity.")
    return _numeric(raw.get("id", ""), "Your user ID")


async def _enrolled_course(identifier: str | int) -> str:
    resolved = await get_course_id(identifier)
    courses = await fetch_all_paginated_results(
        canvas_path("courses"), {"per_page": 100}
    )
    if not isinstance(courses, list):
        raise ValueError("Could not verify your course enrollment.")
    matches = [
        course
        for course in courses
        if isinstance(course, dict) and str(course.get("id")) == str(resolved)
    ]
    if len(matches) != 1:
        raise ValueError(
            "This course is not confirmed in your enrolled-course inventory."
        )
    return _numeric(matches[0]["id"], "course_id")


def _range(start_date: str | None, end_date: str | None) -> dict[str, Any]:
    result: dict[str, Any] = {"per_page": 100}
    dates = {}
    for key, value in (("start_date", start_date), ("end_date", end_date)):
        if value is not None:
            parsed = (
                datetime.fromisoformat(_timestamp(value, key))
                if "T" in value
                else datetime.combine(_day(value, key), datetime.min.time(), UTC)
            )
            result[key] = parsed.isoformat()
            dates[key] = parsed
    if len(dates) == 2 and dates["start_date"] > dates["end_date"]:
        raise ValueError("start_date must not be after end_date.")
    return result


async def _owned_object(
    endpoint: str, object_id: str | int, *, user_scoped: bool
) -> dict[str, Any]:
    raw = await make_canvas_request("get", endpoint)
    if not _record(raw) or str(raw.get("id")) != str(object_id):
        raise ValueError("Could not read your requested planning object.")
    if user_scoped and str(raw.get("user_id")) != await _self_id():
        raise ValueError("The planning object is not confirmed owned by you.")
    return raw


async def _list_objects(
    endpoint: str, kind: str, params: dict[str, Any] | None = None
) -> dict[str, Any]:
    raw = await fetch_all_paginated_results(endpoint, params or {"per_page": 100})
    if not isinstance(raw, list):
        return {"error": f"Could not read your {kind} list."}
    own_id = await _self_id() if kind in {"note", "override"} else None
    return {
        "items": [
            _snapshot(entry, kind)
            for entry in raw
            if _record(entry)
            and (own_id is None or str(entry.get("user_id")) == own_id)
        ]
    }


async def _target_course(
    plannable_type: str,
    plannable_id: str | int,
    target_start_date: str | None = None,
    target_end_date: str | None = None,
) -> str | None:
    params = _range(target_start_date, target_end_date)
    kind = _planner_type(plannable_type)
    if kind not in _TYPES:
        raise ValueError(
            "This planner type is not supported; peer-review and sub-assignment targets are excluded."
        )
    object_id = _numeric(plannable_id, "plannable_id")
    if kind == "planner_note":
        note = await _owned_object(
            canvas_path("planner_notes", object_id), object_id, user_scoped=True
        )
        return (
            await _enrolled_course(note["course_id"])
            if note.get("course_id") is not None
            else None
        )
    items = await fetch_all_paginated_results(canvas_path("planner", "items"), params)
    if not isinstance(items, list):
        raise ValueError("Could not verify this item in your planner feed.")
    matches = [
        item
        for item in items
        if isinstance(item, dict)
        and item.get("plannable_type") == kind
        and str(item.get("plannable_id")) == object_id
    ]
    if not matches or any(
        item.get("course_id") != matches[0].get("course_id") for item in matches
    ):
        raise ValueError(
            "This target is not unambiguously present in your own current planner feed "
            "(Canvas defaults to two weeks before and after today); supply "
            "target_start_date and target_end_date covering the target."
        )
    course = matches[0].get("course_id")
    if course is None:
        if kind != "calendar_event":
            raise ValueError("The planner target did not identify its course.")
        event = await make_canvas_request(
            "get", canvas_path("calendar_events", object_id)
        )
        if (
            not _record(event)
            or event.get("context_code") != f"user_{await _self_id()}"
        ):
            raise ValueError(
                "This calendar item is not confirmed on your personal calendar."
            )
        return None
    return await _enrolled_course(course)


async def _authorize(tool: str, course_id: str | None) -> None:
    if tool not in get_config().student_write_tools:
        raise ValueError(f"{tool} is disabled; student writes default to off.")
    if course_id is not None:
        allowed, reason = await check_student_write_allowed(course_id, tool)
        if not allowed:
            raise ValueError(
                f'This course blocks the write. {fence_untrusted(reason, "course write policy")}'
            )


def _text_payload(**values: str | None) -> dict[str, Any]:
    data = {key: value for key, value in values.items() if value is not None}
    if any(contains_fence_markers(value) for value in data.values()):
        raise ValueError(FENCE_LEAK_ERROR)
    for key in ("title", "name"):
        if key in data and not data[key].strip():
            raise ValueError(f"{key} must not be empty.")
    if "todo_date" in data:
        data["todo_date"] = _day(data["todo_date"], "todo_date").isoformat()
    if "url" in data:
        parsed = urlsplit(data["url"])
        if not (
            (parsed.scheme in {"http", "https"} and parsed.netloc)
            or (
                data["url"].startswith("/")
                and not data["url"].startswith("//")
                and not parsed.scheme
            )
        ):
            raise ValueError(
                "Bookmark URLs must be http(s) URLs or Canvas relative paths."
            )
    assert_no_identity_override(data)
    return data


def _matches(raw: dict[str, Any], data: dict[str, Any]) -> bool:
    for key, value in data.items():
        actual = (
            raw.get("description", raw.get("details"))
            if key == "details"
            else raw.get(key)
        )
        if key == "todo_date" and isinstance(actual, str):
            actual = actual[:10]
        if key == "plannable_type":
            actual = _planner_type(actual)
        if key in {"course_id", "plannable_id"}:
            actual, value = str(actual), str(value)
        if actual != value:
            return False
    return True


async def _mutate(
    tool: str,
    endpoint: str,
    kind: str,
    data: dict[str, Any],
    *,
    method: str,
    current: dict[str, Any] | None = None,
    course_id: str | None = None,
    confirmation_token: str | None = None,
    confirm: bool = False,
) -> dict[str, Any] | str:
    synchronizes_completion = (
        kind == "override" and course_id is not None and method != "delete"
    )
    await _authorize(tool, course_id)
    if synchronizes_completion:
        await _authorize("mark_module_item_done", course_id)
    if confirm:
        fingerprint = _PLANNING_GUARD.fingerprint(
            tool, endpoint, json.dumps([current, data, course_id], sort_keys=True)
        )
        if confirmation_token is None:
            shown = {
                "current": _snapshot(current, kind) if current else None,
                "requested": {
                    key: (
                        fence_untrusted(value, f"requested {key}")
                        if isinstance(value, str)
                        else value
                    )
                    for key, value in data.items()
                },
            }
            return preview_with_token(
                _PLANNING_GUARD,
                fingerprint,
                tool,
                json.dumps(shown)
                + (
                    "\nPlanner completion settings can also change your module completion state."
                    if kind == "override" and method != "delete"
                    else ""
                ),
                action="delete" if method == "delete" else "update",
            )
        error = redeem_confirmation(_PLANNING_GUARD, confirmation_token, fingerprint)
        if error:
            return {"error": error}
    own_id = await _self_id() if kind in {"note", "override"} else None
    await _authorize(tool, course_id)
    if synchronizes_completion:
        await _authorize("mark_module_item_done", course_id)
    assert_no_identity_override(data)
    raw = (
        await make_canvas_request(method, endpoint, data=data)
        if method != "delete"
        else await make_canvas_request(method, endpoint)
    )
    if (
        not _record(raw)
        or coerce_canvas_id(raw.get("id", "")) is None
        or (current and str(raw["id"]) != str(current["id"]))
    ):
        return {
            "error": "The write could not be confirmed. Inspect Canvas before retrying.",
            "write_unconfirmed": True,
        }
    if kind in {"note", "override"} and str(raw.get("user_id")) != own_id:
        return {
            "error": "The written object is not confirmed owned by you.",
            "write_unconfirmed": True,
        }
    if method != "delete" and not _matches(raw, data):
        return {
            "error": "Canvas did not confirm the requested field values.",
            "write_unconfirmed": True,
        }
    return {
        "status": (
            "deleted" if method == "delete" else "updated" if current else "created"
        ),
        "item": _snapshot(raw, kind),
    }


def register_student_planning_tools(mcp: FastMCP) -> None:
    """Register self-only planning reads in student/all profiles."""

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    @validate_params
    async def list_my_planner_items(
        start_date: str | None = None,
        end_date: str | None = None,
        course_identifier: str | int | None = None,
        filter: str | None = None,
    ) -> dict[str, Any]:
        """Read your planner objects, statuses and visibility overrides without observer identity overrides."""
        try:
            params = _range(start_date, end_date)
            if filter is not None:
                if filter not in {"new_activity", "incomplete_items", "complete_items"}:
                    raise ValueError("Unsupported planner filter.")
                params["filter"] = filter
            course = (
                await _enrolled_course(course_identifier)
                if course_identifier is not None
                else None
            )
            if course:
                params["context_codes[]"] = [f"course_{course}"]
            raw = await fetch_all_paginated_results(
                canvas_path("planner", "items"), params
            )
            if not isinstance(raw, list):
                return {"error": "Could not read your planner feed."}
            result = []
            for item in raw:
                if not _record(item) or (
                    course and str(item.get("course_id")) != course
                ):
                    continue
                entry = _scalar_fields(
                    item,
                    ("course_id", "plannable_id", "plannable_type", "plannable_date"),
                )
                plannable = item.get("plannable")
                if isinstance(plannable, dict):
                    entry["plannable"] = _scalar_fields(
                        plannable,
                        (
                            "id",
                            "due_at",
                            "todo_date",
                            "points_possible",
                            "workflow_state",
                        ),
                    )
                    for key in ("title", "name", "details"):
                        if isinstance(plannable.get(key), str):
                            entry["plannable"][key] = fence_untrusted(
                                plannable[key], f"planner {key}"
                            )
                override = item.get("planner_override")
                if isinstance(override, dict):
                    entry["planner_override"] = _snapshot(override, "override")
                if isinstance(item.get("submissions"), dict):
                    entry["submission_status"] = {
                        key: item["submissions"][key]
                        for key in (
                            "excused",
                            "graded",
                            "late",
                            "missing",
                            "needs_grading",
                            "with_feedback",
                        )
                        if isinstance(item["submissions"].get(key), bool)
                    }
                result.append(entry)
            return {"items": result}
        except ValueError as exc:
            return {"error": str(exc)}

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    @validate_params
    async def list_my_planner_notes(
        start_date: str | None = None,
        end_date: str | None = None,
        course_identifier: str | int | None = None,
    ) -> dict[str, Any]:
        """Read your personal/course notes with optional dates and enrolled-course filtering."""
        try:
            params = _range(start_date, end_date)
            course = (
                await _enrolled_course(course_identifier)
                if course_identifier is not None
                else None
            )
            if course:
                params["context_codes[]"] = [f"course_{course}"]
            result = await _list_objects(canvas_path("planner_notes"), "note", params)
            if course and "items" in result:
                result["items"] = [
                    entry
                    for entry in result["items"]
                    if str(entry.get("course_id")) == course
                ]
            return result
        except ValueError as exc:
            return {"error": str(exc)}

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    @validate_params
    async def get_my_planner_note(note_id: str | int) -> dict[str, Any]:
        """Read one note owned by the current user."""
        try:
            nid = _numeric(note_id, "note_id")
            return {
                "item": _snapshot(
                    await _owned_object(
                        canvas_path("planner_notes", nid), nid, user_scoped=True
                    ),
                    "note",
                )
            }
        except ValueError as exc:
            return {"error": str(exc)}

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    @validate_params
    async def list_my_planner_overrides() -> dict[str, Any]:
        """Read your planner completion/dismissal preferences."""
        try:
            return await _list_objects(canvas_path("planner", "overrides"), "override")
        except ValueError as exc:
            return {"error": str(exc)}

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    @validate_params
    async def get_my_planner_override(override_id: str | int) -> dict[str, Any]:
        """Read one planner override owned by the current user."""
        try:
            oid = _numeric(override_id, "override_id")
            return {
                "item": _snapshot(
                    await _owned_object(
                        canvas_path("planner", "overrides", oid), oid, user_scoped=True
                    ),
                    "override",
                )
            }
        except ValueError as exc:
            return {"error": str(exc)}

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    @validate_params
    async def list_my_calendar_events(
        start_date: str | None = None,
        end_date: str | None = None,
        course_identifier: str | int | None = None,
        event_type: str = "event",
        undated: bool = False,
    ) -> dict[str, Any]:
        """Read your personal calendar, or an explicitly selected enrolled course; exclude reservations and participant lists."""
        try:
            if event_type not in {"event", "assignment"}:
                raise ValueError("event_type must be event or assignment.")
            params = _range(start_date, end_date)
            context = (
                f"course_{await _enrolled_course(course_identifier)}"
                if course_identifier is not None
                else f"user_{await _self_id()}"
            )
            params.update(
                {
                    "type": event_type,
                    "undated": undated,
                    "context_codes[]": [context],
                    "excludes[]": ["child_events", "assignment"],
                }
            )
            raw = await fetch_all_paginated_results(
                canvas_path("calendar_events"), params
            )
            if not isinstance(raw, list):
                return {"error": "Could not read your calendar."}
            return {
                "events": [
                    _snapshot(event, "calendar")
                    for event in raw
                    if _record(event)
                    and event.get("context_code") == context
                    and not event.get("appointment_group_id")
                ]
            }
        except ValueError as exc:
            return {"error": str(exc)}

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    @validate_params
    async def list_my_favorite_courses() -> dict[str, Any]:
        """Read dashboard favorites; Canvas returns an enrolled-course fallback if none were selected."""
        return await _list_objects(
            canvas_path("users", "self", "favorites", "courses"), "course"
        )

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    @validate_params
    async def list_my_bookmarks() -> dict[str, Any]:
        """Read your bookmarks without arbitrary associated data."""
        return await _list_objects(
            canvas_path("users", "self", "bookmarks"), "bookmark"
        )

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    @validate_params
    async def get_my_bookmark(bookmark_id: str | int) -> dict[str, Any]:
        """Read one bookmark on the hard-coded users/self route."""
        try:
            bid = _numeric(bookmark_id, "bookmark_id")
            return {
                "item": _snapshot(
                    await _owned_object(
                        canvas_path("users", "self", "bookmarks", bid),
                        bid,
                        user_scoped=False,
                    ),
                    "bookmark",
                )
            }
        except ValueError as exc:
            return {"error": str(exc)}

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    @validate_params
    async def get_my_module_progress(course_identifier: str | int) -> dict[str, Any]:
        """Read your module completion/progression state and all item requirements in an enrolled course."""
        try:
            course = await _enrolled_course(course_identifier)
            raw = await fetch_all_paginated_results(
                canvas_path("courses", course, "modules"), {"per_page": 100}
            )
            if not isinstance(raw, list):
                return {"error": "Could not read your modules."}
            result = []
            for module in raw:
                if not _record(module) or not module.get("id"):
                    continue
                items = await fetch_all_paginated_results(
                    canvas_path("courses", course, "modules", module["id"], "items"),
                    {"per_page": 100},
                )
                if not isinstance(items, list):
                    return {
                        "error": "Could not read every module item; progress is incomplete."
                    }
                entry = _snapshot(module, "module")
                entry["items"] = [
                    _snapshot(item, "item") for item in items if _record(item)
                ]
                result.append(entry)
            return {"course_id": course, "modules": result}
        except ValueError as exc:
            return {"error": str(exc)}

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    @validate_params
    async def get_my_module_item_sequence(
        course_identifier: str | int, item_id: str | int
    ) -> dict[str, Any]:
        """Read previous/current/next items for yourself; mastery-path selection is excluded."""
        try:
            iid = _numeric(item_id, "item_id")
            course = await _enrolled_course(course_identifier)
            raw = await make_canvas_request(
                "get",
                canvas_path("courses", course, "module_item_sequence"),
                params={"asset_type": "ModuleItem", "asset_id": iid},
            )
            if not _record(raw) or not isinstance(raw.get("items"), list):
                return {"error": "Could not read your module item sequence."}
            return {
                "items": [
                    {
                        key: (
                            _snapshot(sequence[key], "item")
                            if isinstance(sequence.get(key), dict)
                            else None
                        )
                        for key in ("prev", "current", "next")
                    }
                    for sequence in raw["items"]
                    if isinstance(sequence, dict)
                ]
            }
        except ValueError as exc:
            return {"error": str(exc)}

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    @validate_params
    async def get_my_submission_history(
        course_identifier: str | int, assignment_id: str | int
    ) -> dict[str, Any]:
        """Read your attempts, submitted text and attachment metadata; no other students, grader identities, or signed URLs."""
        try:
            aid = _numeric(assignment_id, "assignment_id")
            course = await _enrolled_course(course_identifier)
            raw = await make_canvas_request(
                "get",
                canvas_path(
                    "courses", course, "assignments", aid, "submissions", "self"
                ),
                params={"include[]": ["submission_history"]},
            )
            if not _record(raw):
                return {"error": "Could not read your submission history."}
            result: dict[str, Any] = {
                "current": _snapshot(raw, "submission"),
                "history": [],
            }
            if isinstance(raw.get("submission_history"), list):
                result["history"] = [
                    _snapshot(attempt, "submission")
                    for attempt in raw["submission_history"]
                    if _record(attempt)
                ]
            return result
        except ValueError as exc:
            return {"error": str(exc)}

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    @validate_params
    async def get_my_submission_file(
        course_identifier: str | int, assignment_id: str | int, file_id: str | int
    ) -> dict[str, Any]:
        """Read attachment metadata only after confirming it belongs to your submission or attempts."""
        try:
            fid = _numeric(file_id, "file_id")
            aid = _numeric(assignment_id, "assignment_id")
            course = await _enrolled_course(course_identifier)
            raw = await make_canvas_request(
                "get",
                canvas_path(
                    "courses", course, "assignments", aid, "submissions", "self"
                ),
                params={"include[]": ["submission_history"]},
            )
            if not _record(raw):
                return {"error": "Could not verify your submission attachments."}
            attempts = [raw] + (
                raw["submission_history"]
                if isinstance(raw.get("submission_history"), list)
                else []
            )
            found = [
                file
                for attempt in attempts
                if isinstance(attempt, dict)
                and isinstance(attempt.get("attachments"), list)
                for file in attempt["attachments"]
                if isinstance(file, dict) and str(file.get("id")) == fid
            ]
            if not found:
                return {
                    "error": "This file is not confirmed attached to your submission."
                }
            file = await make_canvas_request("get", canvas_path("files", fid))
            if not _record(file) or str(file.get("id")) != fid:
                return {"error": "Could not read the verified attachment metadata."}
            return {"file": _snapshot(file, "file")}
        except ValueError as exc:
            return {"error": str(exc)}


async def _note_change(
    tool: str,
    note_id: str | int,
    data: dict[str, Any],
    confirmation_token: str | None,
    *,
    delete: bool = False,
) -> dict[str, Any] | str:
    nid = _numeric(note_id, "note_id")
    endpoint = canvas_path("planner_notes", nid)
    current = await _owned_object(endpoint, nid, user_scoped=True)
    course = (
        await _enrolled_course(current["course_id"])
        if current.get("course_id") is not None
        else None
    )
    return await _mutate(
        tool,
        endpoint,
        "note",
        data,
        method="delete" if delete else "put",
        current=current,
        course_id=course,
        confirmation_token=confirmation_token,
        confirm=True,
    )


async def _override_change(
    tool: str,
    override_id: str | int,
    marked_complete: bool | None,
    dismissed: bool | None,
    confirmation_token: str | None,
    *,
    delete: bool = False,
    target_start_date: str | None = None,
    target_end_date: str | None = None,
) -> dict[str, Any] | str:
    oid = _numeric(override_id, "override_id")
    endpoint = canvas_path("planner", "overrides", oid)
    current = await _owned_object(endpoint, oid, user_scoped=True)
    course = await _target_course(
        current.get("plannable_type", ""),
        current.get("plannable_id", ""),
        target_start_date,
        target_end_date,
    )
    data = {}
    if not delete:
        if not isinstance(current.get("marked_complete"), bool) or not isinstance(
            current.get("dismissed"), bool
        ):
            raise ValueError(
                "Current planner preferences are missing; a partial update would overwrite them."
            )
        data = {
            "marked_complete": (
                marked_complete
                if marked_complete is not None
                else current["marked_complete"]
            ),
            "dismissed": dismissed if dismissed is not None else current["dismissed"],
        }
    return await _mutate(
        tool,
        endpoint,
        "override",
        data,
        method="delete" if delete else "put",
        current=current,
        course_id=course,
        confirmation_token=confirmation_token,
        confirm=True,
    )


async def _bookmark_change(
    tool: str,
    bookmark_id: str | int,
    data: dict[str, Any],
    confirmation_token: str | None,
    *,
    delete: bool = False,
) -> dict[str, Any] | str:
    bid = _numeric(bookmark_id, "bookmark_id")
    endpoint = canvas_path("users", "self", "bookmarks", bid)
    current = await _owned_object(endpoint, bid, user_scoped=False)
    return await _mutate(
        tool,
        endpoint,
        "bookmark",
        data,
        method="delete" if delete else "put",
        current=current,
        confirmation_token=confirmation_token,
        confirm=True,
    )


async def _favorite_change(
    tool: str,
    course_identifier: str | int,
    confirmation_token: str | None,
    *,
    remove: bool = False,
) -> dict[str, Any] | str:
    course = await _enrolled_course(course_identifier)
    await _authorize(tool, course)
    endpoint = canvas_path("users", "self", "favorites", "courses", course)
    if remove:
        favorites = await fetch_all_paginated_results(
            canvas_path("users", "self", "favorites", "courses"), {"per_page": 100}
        )
        if not isinstance(favorites, list):
            raise ValueError("Could not read your current favorite courses.")
        fingerprint = _PLANNING_GUARD.fingerprint(
            tool, endpoint, json.dumps([course, favorites], sort_keys=True)
        )
        if confirmation_token is None:
            return preview_with_token(
                _PLANNING_GUARD,
                fingerprint,
                tool,
                f"Would remove course {course} from your favorites.",
            )
        error = redeem_confirmation(_PLANNING_GUARD, confirmation_token, fingerprint)
        if error:
            return {"error": error}
    await _authorize(tool, course)
    raw = await make_canvas_request("delete" if remove else "post", endpoint)
    if (
        not _record(raw)
        or raw.get("context_type") != "Course"
        or str(raw.get("context_id")) != course
    ):
        return {
            "error": "Canvas did not confirm your requested favorite change.",
            "write_unconfirmed": True,
        }
    return {"status": "removed" if remove else "added", "course_id": course}


def _personal_event(
    raw: object, context: str, event_id: str | None = None, *, deleted: bool = False
) -> TypeGuard[dict[str, Any]]:
    if not _record(raw) or coerce_canvas_id(raw.get("id", "")) is None:
        return False
    if raw.get("context_code") != context or (
        event_id is not None and str(raw["id"]) != event_id
    ):
        return False
    required = (
        "parent_event_id",
        "child_events_count",
        "series_uuid",
        "rrule",
    )
    if any(key not in raw for key in required):
        return False
    if not isinstance(raw.get("workflow_state"), str) or raw.get(
        "workflow_state"
    ) not in ({"deleted"} if deleted else {"active"}):
        return False
    if any(
        raw.get(key) not in (None, "", False, 0)
        for key in (
            "appointment_group_id",
            "parent_event_id",
            "child_events_count",
            "series_uuid",
            "rrule",
            "series_head",
            "reserve_url",
            "reserved",
            "own_reservation",
            "appointment_group_url",
        )
    ):
        return False
    if (
        raw.get("child_events") not in (None, [])
        or raw.get("user") is not None
        or raw.get("group") is not None
    ):
        return False
    if raw.get("effective_context_code") not in (None, "", context) or raw.get(
        "all_context_codes"
    ) not in (None, "", context):
        return False
    return True


def _timestamp(value: str, label: str) -> str:
    if "T" not in value:
        raise ValueError(f"{label} must be an ISO timestamp with an explicit timezone.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(
            f"{label} must be an ISO timestamp with an explicit timezone."
        ) from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{label} must include an explicit timezone.")
    return parsed.astimezone(UTC).isoformat()


def _calendar_payload(
    *,
    title: str | None,
    description: str | None,
    start_at: str | None,
    end_at: str | None,
    location_name: str | None,
    location_address: str | None,
    all_day: bool | None,
    all_day_date: str | None,
    clear_dates: bool = False,
) -> dict[str, Any]:
    data = _text_payload(
        title=title,
        description=description,
        location_name=location_name,
        location_address=location_address,
    )
    if clear_dates:
        if (
            any(value is not None for value in (start_at, end_at, all_day_date))
            or all_day is True
        ):
            raise ValueError(
                "clear_dates cannot be combined with dates or all_day=true."
            )
        data.update({"start_at": "", "end_at": "", "all_day": False})
        return data
    if all_day_date is not None:
        if start_at is not None or end_at is not None or all_day is False:
            raise ValueError(
                "all_day_date cannot be combined with timestamps or all_day=false."
            )
        day = _day(all_day_date, "all_day_date").isoformat()
        data.update(
            {
                "start_at": f"{day}T00:00:00+00:00",
                "end_at": f"{day}T00:00:00+00:00",
                "all_day": True,
                "time_zone_edited": "UTC",
            }
        )
    elif all_day is True:
        raise ValueError("all_day=true requires all_day_date in YYYY-MM-DD format.")
    else:
        for key, value in (("start_at", start_at), ("end_at", end_at)):
            if value is not None:
                data[key] = _timestamp(value, key)
        if start_at is not None or end_at is not None:
            data["all_day"] = False
        elif all_day is not None:
            data["all_day"] = all_day
    return data


def _calendar_matches(raw: dict[str, Any], data: dict[str, Any]) -> bool:
    for key, value in data.items():
        if key == "time_zone_edited":
            continue
        actual = raw.get(key)
        if key in {"start_at", "end_at"}:
            if value == "":
                if actual not in (None, ""):
                    return False
                continue
            if not isinstance(actual, str):
                return False
            try:
                if _timestamp(actual, key) != value:
                    return False
            except ValueError:
                return False
        elif actual != value:
            return False
    if data.get("all_day") is True and raw.get("all_day_date") != data["start_at"][:10]:
        return False
    return True


async def _calendar_mutate(
    tool: str,
    data: dict[str, Any],
    *,
    event_id: str | int | None = None,
    confirmation_token: str | None = None,
    delete: bool = False,
) -> dict[str, Any] | str:
    await _authorize(tool, None)
    context = f"user_{await _self_id()}"
    current = None
    if event_id is not None:
        eid = _numeric(event_id, "event_id")
        endpoint = canvas_path("calendar_events", eid)
        current = await make_canvas_request("get", endpoint)
        if not _personal_event(current, context, eid):
            raise ValueError(
                "Only confirmed active, standalone personal calendar events can be changed; shared, appointment, series, and incomplete metadata are refused."
            )
    else:
        endpoint = canvas_path("calendar_events")
        eid = None
        data = {"context_code": context, **data}
        if "start_at" in data and "end_at" not in data:
            data["end_at"] = data["start_at"]
        if "end_at" in data and "start_at" not in data:
            raise ValueError(
                "Creating a dated event requires start_at when end_at is supplied."
            )
    if not delete:
        start = data.get("start_at", current.get("start_at") if current else None)
        end = data.get("end_at", current.get("end_at") if current else None)
        if (
            start
            and end
            and datetime.fromisoformat(_timestamp(start, "start_at"))
            > datetime.fromisoformat(_timestamp(end, "end_at"))
        ):
            raise ValueError("start_at must not be after end_at.")
        if end and not start:
            raise ValueError("An event with end_at must also have start_at.")
    if current is not None:
        fingerprint = _PLANNING_GUARD.fingerprint(
            tool, endpoint, json.dumps([context, current, data], sort_keys=True)
        )
        if confirmation_token is None:
            shown = {
                "current": _snapshot(current, "calendar"),
                "requested": {
                    key: (
                        fence_untrusted(value, f"requested calendar {key}")
                        if isinstance(value, str)
                        else value
                    )
                    for key, value in data.items()
                },
            }
            return preview_with_token(
                _PLANNING_GUARD,
                fingerprint,
                tool,
                json.dumps(shown),
                action="delete" if delete else "update",
            )
        error = redeem_confirmation(_PLANNING_GUARD, confirmation_token, fingerprint)
        if error:
            return {"error": error}
    await _authorize(tool, None)
    form = {
        f"calendar_event[{key}]": (
            str(value).lower() if isinstance(value, bool) else value
        )
        for key, value in data.items()
    }
    if event_id is not None:
        form["which"] = "one"
    assert_no_identity_override(form)
    raw = (
        await make_canvas_request("delete", endpoint, params={"which": "one"})
        if delete
        else await make_canvas_request(
            "put" if event_id is not None else "post",
            endpoint,
            data=form,
            use_form_data=True,
        )
    )
    if not _personal_event(raw, context, eid, deleted=delete) or (
        not delete and not _calendar_matches(raw, data)
    ):
        return {
            "error": "Canvas did not confirm the requested standalone personal event change. Inspect your calendar before retrying.",
            "write_unconfirmed": True,
        }
    return {
        "status": (
            "deleted" if delete else "updated" if event_id is not None else "created"
        ),
        "event": _snapshot(raw, "calendar"),
    }


def register_student_planning_write_tools(mcp: FastMCP) -> None:
    """Register only individually operator-enabled student planning writes."""
    enabled = get_config().student_write_tools

    if "create_my_planner_note" in enabled:

        @mcp.tool(
            annotations=ToolAnnotations(destructive_hint=False, idempotent_hint=False)
        )
        @validate_params
        async def create_my_planner_note(
            title: str,
            todo_date: str,
            details: str | None = None,
            course_identifier: str | int | None = None,
        ) -> dict[str, Any] | str:
            """Create a personal note, optionally related to an enrolled course under its agent-write policy."""
            try:
                data = _text_payload(title=title, todo_date=todo_date, details=details)
                course = (
                    await _enrolled_course(course_identifier)
                    if course_identifier is not None
                    else None
                )
                if course is not None:
                    data["course_id"] = course
                return await _mutate(
                    "create_my_planner_note",
                    canvas_path("planner_notes"),
                    "note",
                    data,
                    method="post",
                    course_id=course,
                )
            except ValueError as exc:
                return {"error": str(exc)}

    if "update_my_planner_note" in enabled:

        @mcp.tool(
            annotations=ToolAnnotations(destructive_hint=True, idempotent_hint=True)
        )
        @validate_params
        async def update_my_planner_note(
            note_id: str | int,
            title: str | None = None,
            details: str | None = None,
            todo_date: str | None = None,
            confirmation_token: str | None = None,
        ) -> dict[str, Any] | str:
            """Preview and confirm replacement of your note fields; its course/link association is preserved."""
            try:
                data = _text_payload(title=title, details=details, todo_date=todo_date)
                if not data:
                    raise ValueError("No changes specified.")
                return await _note_change(
                    "update_my_planner_note", note_id, data, confirmation_token
                )
            except ValueError as exc:
                return {"error": str(exc)}

    if "delete_my_planner_note" in enabled:

        @mcp.tool(
            annotations=ToolAnnotations(destructive_hint=True, idempotent_hint=True)
        )
        @validate_params
        async def delete_my_planner_note(
            note_id: str | int, confirmation_token: str | None = None
        ) -> dict[str, Any] | str:
            """Preview and confirm deletion of your note, enforcing its course policy when applicable."""
            try:
                return await _note_change(
                    "delete_my_planner_note",
                    note_id,
                    {},
                    confirmation_token,
                    delete=True,
                )
            except ValueError as exc:
                return {"error": str(exc)}

    if "create_my_planner_override" in enabled:

        @mcp.tool(
            annotations=ToolAnnotations(destructive_hint=True, idempotent_hint=False)
        )
        @validate_params
        async def create_my_planner_override(
            plannable_type: str,
            plannable_id: str | int,
            marked_complete: bool = False,
            dismissed: bool = False,
            target_start_date: str | None = None,
            target_end_date: str | None = None,
            confirmation_token: str | None = None,
        ) -> dict[str, Any] | str:
            """Preview and confirm planner preferences for an item in your feed; completion can also affect your module progress. Use target_start_date/target_end_date for items outside the default recent feed."""
            try:
                kind = _planner_type(plannable_type)
                oid = _numeric(plannable_id, "plannable_id")
                course = await _target_course(
                    kind, oid, target_start_date, target_end_date
                )
                existing = await fetch_all_paginated_results(
                    canvas_path("planner", "overrides"), {"per_page": 100}
                )
                if not isinstance(existing, list):
                    raise ValueError("Could not check your existing planner overrides.")
                if any(
                    isinstance(item, dict)
                    and _planner_type(item.get("plannable_type")) == kind
                    and str(item.get("plannable_id")) == oid
                    for item in existing
                ):
                    raise ValueError(
                        "An override already exists; use update_my_planner_override with its ID."
                    )
                data = {
                    "plannable_type": kind,
                    "plannable_id": oid,
                    "marked_complete": marked_complete,
                    "dismissed": dismissed,
                }
                return await _mutate(
                    "create_my_planner_override",
                    canvas_path("planner", "overrides"),
                    "override",
                    data,
                    method="post",
                    course_id=course,
                    confirmation_token=confirmation_token,
                    confirm=True,
                )
            except ValueError as exc:
                return {"error": str(exc)}

    if "update_my_planner_override" in enabled:

        @mcp.tool(
            annotations=ToolAnnotations(destructive_hint=True, idempotent_hint=True)
        )
        @validate_params
        async def update_my_planner_override(
            override_id: str | int,
            marked_complete: bool | None = None,
            dismissed: bool | None = None,
            target_start_date: str | None = None,
            target_end_date: str | None = None,
            confirmation_token: str | None = None,
        ) -> dict[str, Any] | str:
            """Preview and confirm your planner preferences, preserving the unspecified setting; completion can affect module progress. Use target_start_date/target_end_date for nonrecent targets."""
            try:
                if marked_complete is None and dismissed is None:
                    raise ValueError("No changes specified.")
                return await _override_change(
                    "update_my_planner_override",
                    override_id,
                    marked_complete,
                    dismissed,
                    confirmation_token,
                    target_start_date=target_start_date,
                    target_end_date=target_end_date,
                )
            except ValueError as exc:
                return {"error": str(exc)}

    if "delete_my_planner_override" in enabled:

        @mcp.tool(
            annotations=ToolAnnotations(destructive_hint=True, idempotent_hint=True)
        )
        @validate_params
        async def delete_my_planner_override(
            override_id: str | int,
            confirmation_token: str | None = None,
            target_start_date: str | None = None,
            target_end_date: str | None = None,
        ) -> dict[str, Any] | str:
            """Preview and confirm removal of your planner completion/dismissal preferences; optional target_start_date/target_end_date find nonrecent targets."""
            try:
                return await _override_change(
                    "delete_my_planner_override",
                    override_id,
                    None,
                    None,
                    confirmation_token,
                    delete=True,
                    target_start_date=target_start_date,
                    target_end_date=target_end_date,
                )
            except ValueError as exc:
                return {"error": str(exc)}

    if "add_my_favorite_course" in enabled:

        @mcp.tool(
            annotations=ToolAnnotations(destructive_hint=False, idempotent_hint=True)
        )
        @validate_params
        async def add_my_favorite_course(
            course_identifier: str | int,
        ) -> dict[str, Any] | str:
            """Add an enrolled course to your dashboard favorites, subject to its agent-write policy."""
            try:
                return await _favorite_change(
                    "add_my_favorite_course", course_identifier, None
                )
            except ValueError as exc:
                return {"error": str(exc)}

    if "remove_my_favorite_course" in enabled:

        @mcp.tool(
            annotations=ToolAnnotations(destructive_hint=True, idempotent_hint=True)
        )
        @validate_params
        async def remove_my_favorite_course(
            course_identifier: str | int, confirmation_token: str | None = None
        ) -> dict[str, Any] | str:
            """Preview and confirm removal from your course favorites, subject to its agent-write policy."""
            try:
                return await _favorite_change(
                    "remove_my_favorite_course",
                    course_identifier,
                    confirmation_token,
                    remove=True,
                )
            except ValueError as exc:
                return {"error": str(exc)}

    if "create_my_bookmark" in enabled:

        @mcp.tool(
            annotations=ToolAnnotations(destructive_hint=False, idempotent_hint=False)
        )
        @validate_params
        async def create_my_bookmark(
            name: str, url: str, position: int | None = None
        ) -> dict[str, Any] | str:
            """Create your personal bookmark; arbitrary associated data and identity fields are excluded."""
            try:
                data = _text_payload(name=name, url=url)
                if position is not None:
                    if position < 1:
                        raise ValueError("position must be positive.")
                    data["position"] = position
                return await _mutate(
                    "create_my_bookmark",
                    canvas_path("users", "self", "bookmarks"),
                    "bookmark",
                    data,
                    method="post",
                )
            except ValueError as exc:
                return {"error": str(exc)}

    if "update_my_bookmark" in enabled:

        @mcp.tool(
            annotations=ToolAnnotations(destructive_hint=True, idempotent_hint=True)
        )
        @validate_params
        async def update_my_bookmark(
            bookmark_id: str | int,
            name: str | None = None,
            url: str | None = None,
            position: int | None = None,
            confirmation_token: str | None = None,
        ) -> dict[str, Any] | str:
            """Preview and confirm replacement of your bookmark fields."""
            try:
                data = _text_payload(name=name, url=url)
                if position is not None:
                    if position < 1:
                        raise ValueError("position must be positive.")
                    data["position"] = position
                if not data:
                    raise ValueError("No changes specified.")
                return await _bookmark_change(
                    "update_my_bookmark", bookmark_id, data, confirmation_token
                )
            except ValueError as exc:
                return {"error": str(exc)}

    if "delete_my_bookmark" in enabled:

        @mcp.tool(
            annotations=ToolAnnotations(destructive_hint=True, idempotent_hint=True)
        )
        @validate_params
        async def delete_my_bookmark(
            bookmark_id: str | int, confirmation_token: str | None = None
        ) -> dict[str, Any] | str:
            """Preview and confirm deletion of your personal bookmark."""
            try:
                return await _bookmark_change(
                    "delete_my_bookmark",
                    bookmark_id,
                    {},
                    confirmation_token,
                    delete=True,
                )
            except ValueError as exc:
                return {"error": str(exc)}

    if "create_my_calendar_event" in enabled:

        @mcp.tool(
            annotations=ToolAnnotations(destructive_hint=False, idempotent_hint=False)
        )
        @validate_params
        async def create_my_calendar_event(
            title: str,
            description: str | None = None,
            start_at: str | None = None,
            end_at: str | None = None,
            location_name: str | None = None,
            location_address: str | None = None,
            all_day: bool | None = None,
            all_day_date: str | None = None,
        ) -> dict[str, Any] | str:
            """Create a standalone event only on your personal calendar.

            Timed events require ISO timestamps with explicit timezones.
            All-day events use all_day_date=YYYY-MM-DD; times are excluded.
            """
            try:
                data = _calendar_payload(
                    title=title,
                    description=description,
                    start_at=start_at,
                    end_at=end_at,
                    location_name=location_name,
                    location_address=location_address,
                    all_day=all_day,
                    all_day_date=all_day_date,
                )
                return await _calendar_mutate("create_my_calendar_event", data)
            except ValueError as exc:
                return {"error": str(exc)}

    if "update_my_calendar_event" in enabled:

        @mcp.tool(
            annotations=ToolAnnotations(destructive_hint=True, idempotent_hint=True)
        )
        @validate_params
        async def update_my_calendar_event(
            event_id: str | int,
            title: str | None = None,
            description: str | None = None,
            start_at: str | None = None,
            end_at: str | None = None,
            location_name: str | None = None,
            location_address: str | None = None,
            all_day: bool | None = None,
            all_day_date: str | None = None,
            clear_dates: bool = False,
            confirmation_token: str | None = None,
        ) -> dict[str, Any] | str:
            """Preview and confirm changes to your standalone personal event.

            Omitted fields are preserved; empty description/location clears text.
            Use clear_dates=true to make the event undated, not empty timestamps.
            Shared/appointment/recurring events and context changes are excluded.
            """
            try:
                data = _calendar_payload(
                    title=title,
                    description=description,
                    start_at=start_at,
                    end_at=end_at,
                    location_name=location_name,
                    location_address=location_address,
                    all_day=all_day,
                    all_day_date=all_day_date,
                    clear_dates=clear_dates,
                )
                if not data:
                    raise ValueError("No changes specified.")
                return await _calendar_mutate(
                    "update_my_calendar_event",
                    data,
                    event_id=event_id,
                    confirmation_token=confirmation_token,
                )
            except ValueError as exc:
                return {"error": str(exc)}

    if "delete_my_calendar_event" in enabled:

        @mcp.tool(
            annotations=ToolAnnotations(destructive_hint=True, idempotent_hint=True)
        )
        @validate_params
        async def delete_my_calendar_event(
            event_id: str | int, confirmation_token: str | None = None
        ) -> dict[str, Any] | str:
            """Preview and confirm deletion of your standalone personal event; shared/appointment/series events are refused."""
            try:
                return await _calendar_mutate(
                    "delete_my_calendar_event",
                    {},
                    event_id=event_id,
                    confirmation_token=confirmation_token,
                    delete=True,
                )
            except ValueError as exc:
                return {"error": str(exc)}
