"""Confirmed educator assessment management without enrollment mutations."""

from __future__ import annotations

import json
import math
from typing import Any

from fastmcp import FastMCP
from mcp.types import ToolAnnotations

from ..core.client import fetch_all_paginated_results, make_canvas_request
from ..core.config import get_config
from ..core.dates import parse_date
from ..core.path import canvas_path
from ..core.untrusted_content import contains_fence_markers, fence_untrusted
from ..core.validation import coerce_canvas_id, validate_params
from ..core.write_confirmation import ConfirmationGuard, redeem_confirmation
from .content_migrations import _resolve_course

_GUARD = ConfirmationGuard(nothing_done="The requested assessment change was not made.")
_OVERRIDE_KEYS = (
    "id",
    "assignment_id",
    "student_ids",
    "group_id",
    "course_section_id",
    "title",
    "due_at",
    "unlock_at",
    "lock_at",
)
_SECTION_KEYS = (
    "id",
    "course_id",
    "name",
    "start_at",
    "end_at",
    "restrict_enrollments_to_section_dates",
    "total_students",
)
_LATE_KEYS = (
    "missing_submission_deduction_enabled",
    "missing_submission_deduction",
    "late_submission_deduction_enabled",
    "late_submission_deduction",
    "late_submission_interval",
    "late_submission_minimum_percent_enabled",
    "late_submission_minimum_percent",
)
_SUBMISSION_KEYS = (
    "id",
    "assignment_id",
    "user_id",
    "attempt",
    "submission_type",
    "workflow_state",
    "submitted_at",
    "graded_at",
    "score",
    "grade",
    "late",
    "missing",
    "excused",
    "late_policy_status",
    "seconds_late",
    "points_deducted",
    "posted_at",
)


def _pick(record: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    return {key: record[key] for key in keys if key in record}


def _shown(value: Any) -> Any:
    if isinstance(value, list):
        return [_shown(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {}
    for key, item in value.items():
        if key == "title" and get_config().enable_data_anonymization:
            result[key] = "Assignment override"
        elif key in {
            "title",
            "name",
            "comment",
            "body",
            "url",
            "filename",
            "author_name",
            "grade",
        } and isinstance(item, str):
            result[key] = fence_untrusted(item, f"assessment {key}")
        else:
            result[key] = _shown(item)
    return result


async def _context(
    course_identifier: str | int, *identifiers: str | int, educator: bool = False
) -> tuple[str, list[str], str | None]:
    ids = []
    for value in identifiers:
        normalized = coerce_canvas_id(value)
        if normalized is None or int(normalized) < 1:
            return "", [], "Resource identifiers must be positive numeric Canvas IDs."
        ids.append(normalized)
    course_id, _course, error = await _resolve_course(course_identifier, "assessment")
    if error or course_id is None:
        return "", [], "Could not verify the assessment course."
    if educator:
        permissions = await make_canvas_request(
            "get",
            canvas_path("courses", course_id, "permissions"),
            params={"permissions[]": "manage_grades"},
        )
        if (
            not isinstance(permissions, dict)
            or "error" in permissions
            or not (
                permissions.get("manage_grades") is True
                or permissions.get("manage_grades") == "true"
            )
        ):
            return (
                "",
                [],
                "Verified manage_grades permission is required for this educator action.",
            )
    return course_id, ids, None


def _confirmation(
    action: str,
    course_id: str,
    current: Any,
    payload: dict[str, Any],
    token: str | None,
) -> dict[str, Any] | None:
    fingerprint = _GUARD.fingerprint(
        action,
        course_id,
        json.dumps(current, sort_keys=True),
        json.dumps(payload, sort_keys=True),
    )
    if not token:
        return {
            "preview": True,
            "changed": False,
            "course_id": course_id,
            "action": action,
            "current": _shown(current),
            "requested": _shown(payload),
            "confirmation_token": _GUARD.issue(fingerprint),
            "instructions": "Show this preview to the educator and confirm only after approval, using identical arguments and the confirmation_token.",
        }
    error = redeem_confirmation(_GUARD, token, fingerprint)
    return {"error": error, "changed": False} if error else None


def _dates(
    values: dict[str, str | None],
    clears: dict[str, bool],
    inherited: dict[str, bool] | None = None,
) -> tuple[dict[str, Any], str | None]:
    result: dict[str, Any] = {}
    for field, value in values.items():
        clear = clears.get(field, False)
        inherit = (inherited or {}).get(field, False)
        if sum((value is not None, clear, inherit)) > 1:
            return (
                {},
                f"Specify only one value, clear flag or inherit flag for {field}.",
            )
        if clear:
            result[field] = None
        elif value is not None:
            parsed = parse_date(value)
            if parsed is None or parsed.tzinfo is None:
                return {}, f"{field} must be an ISO 8601 date with a timezone."
            result[field] = parsed.isoformat()
    return result, None


def _date_order(
    payload: dict[str, Any], start: str = "unlock_at", end: str = "lock_at"
) -> str | None:
    for key in (start, end, "due_at"):
        value = payload.get(key)
        if value is not None:
            parsed_value = parse_date(value) if isinstance(value, str) else None
            if (
                parsed_value is None
                or parsed_value.tzinfo is None
                or contains_fence_markers(value)
            ):
                return f"The current {key} value is invalid; re-read the resource before updating."
    parsed = {
        key: parse_date(value)
        for key, value in payload.items()
        if key in {start, end, "due_at"} and isinstance(value, str)
    }
    first, last, due = parsed.get(start), parsed.get(end), parsed.get("due_at")
    if first and last and first >= last:
        return f"{start} must be before {end}."
    if due and ((first and due < first) or (last and due > last)):
        return "due_at must lie within the override availability dates."
    return None


def _same(actual: Any, expected: Any, key: str) -> bool:
    if key.endswith("_at") and isinstance(actual, str) and isinstance(expected, str):
        first, second = parse_date(actual), parse_date(expected)
        return first is not None and second is not None and first == second
    if key == "student_ids" and isinstance(actual, list) and isinstance(expected, list):
        return sorted(str(item) for item in actual) == sorted(
            str(item) for item in expected
        )
    return bool(actual == expected)


async def _write_verified(
    method: str,
    endpoint: str,
    payload: dict[str, Any],
    root: str,
    read_endpoint: str | None = None,
) -> dict[str, Any]:
    try:
        response = await make_canvas_request(method, endpoint, data={root: payload})
    except Exception:
        return {
            "warning": "The write outcome is uncertain; inspect the resource before retrying.",
            "verified": False,
        }
    if isinstance(response, dict) and "error" in response:
        return {
            "error": "Canvas did not confirm the requested write; inspect the resource before retrying.",
            "verified": False,
        }
    if read_endpoint is None:
        if (
            not isinstance(response, dict)
            or coerce_canvas_id(response.get("id", "")) is None
        ):
            return {
                "warning": "Canvas did not return a resource ID; inspect the resource list before retrying.",
                "verified": False,
            }
        read_endpoint = endpoint + "/" + str(response["id"])
    verified = await make_canvas_request("get", read_endpoint)
    actual = verified.get(root, verified) if isinstance(verified, dict) else None
    if (
        not isinstance(actual, dict)
        or "error" in actual
        or any(not _same(actual.get(key), value, key) for key, value in payload.items())
    ):
        return {
            "warning": "The requested changes could not be verified by readback.",
            "verified": False,
        }
    parts = read_endpoint.strip("/").split("/")
    expected_course_id = parts[1] if len(parts) > 1 and parts[0] == "courses" else None
    identity_matches = expected_course_id is not None
    if root == "assignment_override":
        identity_matches = (
            identity_matches
            and str(actual.get("id")) == parts[-1]
            and str(actual.get("assignment_id")) == parts[3]
        )
        if "course_id" in actual:
            identity_matches = (
                identity_matches and str(actual["course_id"]) == expected_course_id
            )
    elif root == "course_section":
        identity_matches = (
            identity_matches
            and str(actual.get("id")) == parts[-1]
            and str(actual.get("course_id")) == expected_course_id
        )
    elif root == "late_policy":
        identity_matches = (
            identity_matches and str(actual.get("course_id")) == expected_course_id
        )
    if not identity_matches:
        return {
            "warning": "The changed resource identity could not be verified by readback.",
            "verified": False,
        }
    keys = (
        _OVERRIDE_KEYS
        if root == "assignment_override"
        else _SECTION_KEYS
        if root == "course_section"
        else ("id", "course_id") + _LATE_KEYS
    )
    return {"changed": True, "verified": True, "resource": _shown(_pick(actual, keys))}


async def _override(
    course_id: str, assignment_id: str, override_id: str
) -> dict[str, Any] | None:
    response = await make_canvas_request(
        "get",
        canvas_path(
            "courses", course_id, "assignments", assignment_id, "overrides", override_id
        ),
    )
    if (
        not isinstance(response, dict)
        or "error" in response
        or str(response.get("id")) != override_id
        or str(response.get("assignment_id")) != assignment_id
    ):
        return None
    return _pick(response, _OVERRIDE_KEYS)


async def _section(course_id: str, section_id: str) -> dict[str, Any] | None:
    response = await make_canvas_request(
        "get",
        canvas_path("courses", course_id, "sections", section_id),
        params={"include[]": ["total_students"]},
    )
    if (
        not isinstance(response, dict)
        or "error" in response
        or str(response.get("id")) != section_id
        or str(response.get("course_id")) != course_id
    ):
        return None
    return response


async def _validate_target(
    course_id: str,
    assignment_id: str,
    student_ids: list[int] | None,
    section_id: int | None,
    group_id: int | None,
    title: str | None,
) -> tuple[dict[str, Any], str | None]:
    if sum(value is not None for value in (student_ids, section_id, group_id)) != 1:
        return (
            {},
            "Choose exactly one student_ids, course_section_id or group_id target.",
        )
    assignment = await make_canvas_request(
        "get", canvas_path("courses", course_id, "assignments", assignment_id)
    )
    if (
        not isinstance(assignment, dict)
        or "error" in assignment
        or str(assignment.get("id")) != assignment_id
        or str(assignment.get("course_id")) != course_id
    ):
        return {}, "Could not verify the assignment belongs to this course."
    if student_ids is not None:
        if (
            not student_ids
            or any(type(item) is not int or item < 1 for item in student_ids)
            or len(set(student_ids)) != len(student_ids)
        ):
            return {}, "student_ids must contain unique positive Canvas IDs."
        if not title or contains_fence_markers(title):
            return {}, "An unfenced title is required for a student override."
        return {"student_ids": student_ids, "title": title}, None
    if title is not None:
        return {}, "Titles are supported only for student overrides."
    if section_id is not None:
        if section_id < 1 or await _section(course_id, str(section_id)) is None:
            return {}, "The target section must belong to this course."
        return {"course_section_id": section_id}, None
    if group_id is None or group_id < 1 or not assignment.get("group_category_id"):
        return {}, "A group override requires a group assignment and positive group ID."
    group = await make_canvas_request("get", canvas_path("groups", group_id))
    if (
        not isinstance(group, dict)
        or "error" in group
        or str(group.get("id")) != str(group_id)
        or str(group.get("course_id")) != course_id
        or str(group.get("group_category_id"))
        != str(assignment.get("group_category_id"))
    ):
        return (
            {},
            "The target group must belong to this course and the assignment's group set.",
        )
    return {"group_id": group_id}, None


async def _outcome_page(
    course_identifier: str | int,
    kind: str,
    user_ids: list[int] | None,
    outcome_ids: list[int] | None,
    page: int,
    per_page: int,
    aggregate: str | None = None,
    aggregate_stat: str | None = None,
) -> dict[str, Any]:
    if page < 1 or not 1 <= per_page <= 100:
        return {"error": "page must be positive and per_page between 1 and 100."}
    for label, values in (("user_ids", user_ids), ("outcome_ids", outcome_ids)):
        if values is not None and (
            not values
            or len(values) > 100
            or len(set(values)) != len(values)
            or any(type(value) is not int or value < 1 for value in values)
        ):
            return {
                "error": f"{label} must contain 1..100 unique positive numeric Canvas IDs."
            }
    if (
        aggregate not in (None, "course")
        or aggregate_stat not in (None, "mean", "median")
        or (aggregate_stat is not None and aggregate is None)
    ):
        return {
            "error": "aggregate must be course; aggregate_stat mean/median requires an aggregate."
        }
    course_id, _ids, error = await _context(course_identifier, educator=True)
    if error:
        return {"error": error}
    params: dict[str, Any] = {"page": page, "per_page": per_page}
    if user_ids is not None:
        params["user_ids[]"] = user_ids
    if outcome_ids is not None:
        params["outcome_ids[]"] = outcome_ids
    if kind == "outcome_results":
        params["include_hidden"] = False
    if aggregate is not None:
        params["aggregate"] = aggregate
    if aggregate_stat is not None:
        params["aggregate_stat"] = aggregate_stat
    pagination: dict[str, str | None] = {}
    response = await make_canvas_request(
        "get",
        canvas_path("courses", course_id, kind),
        params=params,
        _pagination=pagination,
    )
    key = "outcome_results" if kind == "outcome_results" else "rollups"
    if (
        not isinstance(response, dict)
        or "error" in response
        or not isinstance(response.get(key), list)
    ):
        return {"error": "Canvas did not return a valid outcome-results page."}
    records = []
    for item in response[key][:per_page]:
        if not isinstance(item, dict):
            continue
        record = (
            _pick(item, ("id", "score", "percent", "submitted_or_assessed_at"))
            if kind == "outcome_results"
            else {}
        )
        record = {
            field: value
            for field, value in record.items()
            if value is None or isinstance(value, (str, int, float, bool))
        }
        links = item.get("links")
        allowed_links = (
            ("user", "learning_outcome", "alignment")
            if kind == "outcome_results"
            else ("user", "course", "section")
        )
        if isinstance(links, dict):
            record["links"] = {
                field: links[field]
                for field in allowed_links
                if field in links and coerce_canvas_id(links[field]) is not None
            }
        if kind == "outcome_rollups":
            scores = []
            for score in item.get("scores") or []:
                if not isinstance(score, dict):
                    continue
                shown_score: dict[str, Any] = {
                    field: value
                    for field, value in _pick(score, ("score", "count")).items()
                    if value is None or isinstance(value, (int, float))
                }
                score_links = score.get("links")
                if (
                    isinstance(score_links, dict)
                    and coerce_canvas_id(score_links.get("outcome", "")) is not None
                ):
                    shown_score["links"] = {"outcome": score_links["outcome"]}
                scores.append(shown_score)
            record["scores"] = scores[:100]
            record["score_count"] = len(scores)
            record["scores_truncated"] = len(scores) > 100
        records.append(record)
    has_more = bool(pagination.get("next"))
    return {
        "course_id": course_id,
        key: records,
        "page": page,
        "per_page": per_page,
        "has_more": has_more,
        "next_page": page + 1 if has_more else None,
        "scope": "visible outcome results",
        "linked_objects_omitted": True,
        "page_truncated": len(response[key]) > per_page,
    }


def register_educator_assessment_tools(mcp: FastMCP) -> None:
    """Register educator/all assessment tools, excluding creator and student profiles."""

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    @validate_params
    async def list_assignment_overrides(
        course_identifier: str | int, assignment_id: str | int
    ) -> dict[str, Any]:
        """List visible section, group and student override dates without embedded users."""
        course_id, ids, error = await _context(course_identifier, assignment_id)
        if error:
            return {"error": error}
        response = await fetch_all_paginated_results(
            canvas_path("courses", course_id, "assignments", ids[0], "overrides"),
            {"per_page": 100},
        )
        if not isinstance(response, list):
            return {"error": "Could not list assignment overrides."}
        return {
            "course_id": course_id,
            "overrides": [
                _shown(_pick(item, _OVERRIDE_KEYS))
                for item in response
                if isinstance(item, dict)
            ],
        }

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    @validate_params
    async def get_assignment_override(
        course_identifier: str | int, assignment_id: str | int, override_id: str | int
    ) -> dict[str, Any]:
        """Read one course-owned override; titles are withheld under anonymization."""
        course_id, ids, error = await _context(
            course_identifier, assignment_id, override_id
        )
        if error:
            return {"error": error}
        current = await _override(course_id, *ids)
        return (
            {"override": _shown(current)}
            if current is not None
            else {"error": "Could not verify the assignment override."}
        )

    @mcp.tool(annotations=ToolAnnotations(destructive_hint=True, idempotent_hint=False))
    @validate_params
    async def create_assignment_override(
        course_identifier: str | int,
        assignment_id: str | int,
        student_ids: list[int] | None = None,
        course_section_id: int | None = None,
        group_id: int | None = None,
        title: str | None = None,
        due_at: str | None = None,
        unlock_at: str | None = None,
        lock_at: str | None = None,
        clear_due_at: bool = False,
        clear_unlock_at: bool = False,
        clear_lock_at: bool = False,
        confirmation_token: str | None = None,
    ) -> dict[str, Any]:
        """Preview/confirm one explicit student, course-section or assignment-group override.

        Student overrides require a title. Canvas verifies active student enrollment
        and conflicts with existing targets. Clear flags explicitly override a date
        to null; omitted dates inherit the assignment defaults.
        """
        course_id, ids, error = await _context(
            course_identifier, assignment_id, educator=True
        )
        if error:
            return {"error": error}
        target, error = await _validate_target(
            course_id, ids[0], student_ids, course_section_id, group_id, title
        )
        dates, date_error = _dates(
            {"due_at": due_at, "unlock_at": unlock_at, "lock_at": lock_at},
            {
                "due_at": clear_due_at,
                "unlock_at": clear_unlock_at,
                "lock_at": clear_lock_at,
            },
        )
        payload = target | dates
        error = error or date_error or _date_order(payload)
        if error:
            return {"error": error}
        existing = await fetch_all_paginated_results(
            canvas_path("courses", course_id, "assignments", ids[0], "overrides"),
            {"per_page": 100},
        )
        if not isinstance(existing, list):
            return {"error": "Could not inspect current overrides before creation."}
        preview = _confirmation(
            "create_assignment_override",
            course_id,
            {
                "assignment_id": ids[0],
                "overrides": [
                    _pick(item, _OVERRIDE_KEYS)
                    for item in existing
                    if isinstance(item, dict)
                ],
            },
            payload,
            confirmation_token,
        )
        if preview:
            return preview
        return await _write_verified(
            "post",
            canvas_path("courses", course_id, "assignments", ids[0], "overrides"),
            payload,
            "assignment_override",
        )

    @mcp.tool(annotations=ToolAnnotations(destructive_hint=True, idempotent_hint=True))
    @validate_params
    async def update_assignment_override(
        course_identifier: str | int,
        assignment_id: str | int,
        override_id: str | int,
        student_ids: list[int] | None = None,
        title: str | None = None,
        due_at: str | None = None,
        unlock_at: str | None = None,
        lock_at: str | None = None,
        clear_due_at: bool = False,
        clear_unlock_at: bool = False,
        clear_lock_at: bool = False,
        inherit_due_at: bool = False,
        inherit_unlock_at: bool = False,
        inherit_lock_at: bool = False,
        confirmation_token: str | None = None,
    ) -> dict[str, Any]:
        """Preview/confirm changes while preserving omitted current overridden dates.

        Only student overrides can change student_ids or title. Clear flags set a
        null override; inherit flags remove that override and restore base dates.
        Group/section targets cannot change. Anonymized titles are not roundtripped.
        """
        course_id, ids, error = await _context(
            course_identifier, assignment_id, override_id, educator=True
        )
        if error:
            return {"error": error}
        current = await _override(course_id, *ids)
        if current is None:
            return {"error": "Could not verify the assignment override."}
        if title is not None and contains_fence_markers(title):
            return {"error": "Override titles must not contain fence markers."}
        if (
            student_ids is not None or title is not None
        ) and "student_ids" not in current:
            return {
                "error": "Group and section override targets/titles cannot be changed."
            }
        if student_ids is not None and (
            not student_ids
            or any(type(item) is not int or item < 1 for item in student_ids)
            or len(set(student_ids)) != len(student_ids)
        ):
            return {"error": "student_ids must contain unique positive Canvas IDs."}
        inherited = {
            "due_at": inherit_due_at,
            "unlock_at": inherit_unlock_at,
            "lock_at": inherit_lock_at,
        }
        dates, error = _dates(
            {"due_at": due_at, "unlock_at": unlock_at, "lock_at": lock_at},
            {
                "due_at": clear_due_at,
                "unlock_at": clear_unlock_at,
                "lock_at": clear_lock_at,
            },
            inherited,
        )
        if error:
            return {"error": error}
        payload: dict[str, Any] = {
            key: current[key]
            for key in ("due_at", "unlock_at", "lock_at")
            if key in current and not inherited[key]
        }
        payload.update(dates)
        if student_ids is not None:
            payload["student_ids"] = student_ids
        if title is not None:
            if not title:
                return {"error": "Student override titles must not be empty."}
            payload["title"] = title
        if (
            not dates
            and student_ids is None
            and title is None
            and not any(inherited.values())
        ):
            return {"error": "No override fields were provided to change."}
        error = _date_order(payload)
        if error:
            return {"error": error}
        preview = _confirmation(
            "update_assignment_override",
            course_id,
            current,
            payload,
            confirmation_token,
        )
        if preview:
            return preview
        endpoint = canvas_path(
            "courses", course_id, "assignments", ids[0], "overrides", ids[1]
        )
        result = await _write_verified(
            "put", endpoint, payload, "assignment_override", endpoint
        )
        if result.get("verified") and any(inherited.values()):
            verified = await _override(course_id, *ids)
            if verified is None or any(
                field in verified for field, inherit in inherited.items() if inherit
            ):
                return {
                    "warning": "The inherited-date changes could not be verified.",
                    "verified": False,
                }
        return result

    @mcp.tool(annotations=ToolAnnotations(destructive_hint=True, idempotent_hint=True))
    @validate_params
    async def delete_assignment_override(
        course_identifier: str | int,
        assignment_id: str | int,
        override_id: str | int,
        confirmation_token: str | None = None,
    ) -> dict[str, Any]:
        """Preview/confirm removing one override, restoring assignment defaults for its target."""
        course_id, ids, error = await _context(
            course_identifier, assignment_id, override_id, educator=True
        )
        if error:
            return {"error": error}
        current = await _override(course_id, *ids)
        if current is None:
            return {"error": "Could not verify the assignment override."}
        preview = _confirmation(
            "delete_assignment_override",
            course_id,
            current,
            {"override_id": ids[1]},
            confirmation_token,
        )
        if preview:
            return preview
        response = await make_canvas_request(
            "delete",
            canvas_path(
                "courses", course_id, "assignments", ids[0], "overrides", ids[1]
            ),
        )
        if (
            not isinstance(response, dict)
            or "error" in response
            or str(response.get("id")) != ids[1]
        ):
            return {
                "warning": "Override deletion could not be confirmed; inspect current overrides before retrying.",
                "deleted": False,
            }
        return {"deleted": True, "override_id": ids[1]}

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    @validate_params
    async def list_course_sections(course_identifier: str | int) -> dict[str, Any]:
        """List section settings and student counts; never return embedded rosters or SIS IDs."""
        course_id, _ids, error = await _context(course_identifier)
        if error:
            return {"error": error}
        sections = await fetch_all_paginated_results(
            canvas_path("courses", course_id, "sections"),
            {"per_page": 100, "include[]": ["total_students"]},
        )
        if not isinstance(sections, list):
            return {"error": "Could not list course sections."}
        return {
            "course_id": course_id,
            "sections": [
                _shown(_pick(item, _SECTION_KEYS))
                for item in sections
                if isinstance(item, dict)
            ],
        }

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    @validate_params
    async def get_course_section(
        course_identifier: str | int, section_id: str | int
    ) -> dict[str, Any]:
        """Read one verified course section without students, enrollment records or SIS IDs."""
        course_id, ids, error = await _context(course_identifier, section_id)
        if error:
            return {"error": error}
        current = await _section(course_id, ids[0])
        return (
            {"section": _shown(_pick(current, _SECTION_KEYS))}
            if current is not None
            else {"error": "Could not verify the course section."}
        )

    @mcp.tool(annotations=ToolAnnotations(destructive_hint=True, idempotent_hint=False))
    @validate_params
    async def create_course_section(
        course_identifier: str | int,
        name: str,
        start_at: str | None = None,
        end_at: str | None = None,
        restrict_enrollments_to_section_dates: bool = False,
        confirmation_token: str | None = None,
    ) -> dict[str, Any]:
        """Preview/confirm an empty section with bounded name/date settings; never enroll users."""
        if not name.strip() or contains_fence_markers(name):
            return {"error": "Provide a nonempty section name without fence markers."}
        dates, error = _dates({"start_at": start_at, "end_at": end_at}, {})
        error = error or _date_order(dates, "start_at", "end_at")
        if error:
            return {"error": error}
        course_id, _ids, error = await _context(course_identifier, educator=True)
        if error:
            return {"error": error}
        payload = {
            "name": name,
            "restrict_enrollments_to_section_dates": restrict_enrollments_to_section_dates,
        } | dates
        sections = await fetch_all_paginated_results(
            canvas_path("courses", course_id, "sections"), {"per_page": 100}
        )
        if not isinstance(sections, list):
            return {"error": "Could not inspect current course sections."}
        preview = _confirmation(
            "create_course_section",
            course_id,
            [_pick(item, _SECTION_KEYS) for item in sections if isinstance(item, dict)],
            payload,
            confirmation_token,
        )
        if preview:
            return preview
        return await _write_verified(
            "post",
            canvas_path("courses", course_id, "sections"),
            payload,
            "course_section",
        )

    @mcp.tool(annotations=ToolAnnotations(destructive_hint=True, idempotent_hint=True))
    @validate_params
    async def update_course_section(
        course_identifier: str | int,
        section_id: str | int,
        name: str | None = None,
        start_at: str | None = None,
        end_at: str | None = None,
        clear_start_at: bool = False,
        clear_end_at: bool = False,
        restrict_enrollments_to_section_dates: bool | None = None,
        confirmation_token: str | None = None,
    ) -> dict[str, Any]:
        """Preview/confirm local section name/date changes without overriding SIS stickiness."""
        if name is not None and (not name.strip() or contains_fence_markers(name)):
            return {"error": "Provide a nonempty section name without fence markers."}
        payload, error = _dates(
            {"start_at": start_at, "end_at": end_at},
            {"start_at": clear_start_at, "end_at": clear_end_at},
        )
        if error:
            return {"error": error}
        if name is not None:
            payload["name"] = name
        if restrict_enrollments_to_section_dates is not None:
            payload["restrict_enrollments_to_section_dates"] = (
                restrict_enrollments_to_section_dates
            )
        if not payload:
            return {"error": "No section settings were supplied."}
        course_id, ids, error = await _context(
            course_identifier, section_id, educator=True
        )
        if error:
            return {"error": error}
        current = await _section(course_id, ids[0])
        if current is None:
            return {"error": "Could not verify the course section."}
        if any(
            current.get(key)
            for key in (
                "sis_section_id",
                "sis_import_id",
                "integration_id",
                "nonxlist_course_id",
            )
        ):
            return {
                "error": "SIS-managed and cross-listed section changes are unsupported."
            }
        error = _date_order(
            _pick(current, _SECTION_KEYS) | payload, "start_at", "end_at"
        )
        if error:
            return {"error": error}
        preview = _confirmation(
            "update_course_section",
            course_id,
            _pick(current, _SECTION_KEYS),
            payload,
            confirmation_token,
        )
        if preview:
            return preview
        endpoint = canvas_path("sections", ids[0])
        response = await make_canvas_request(
            "put",
            endpoint,
            data={"course_section": payload, "override_sis_stickiness": False},
        )
        if not isinstance(response, dict) or "error" in response:
            return {
                "warning": "Section update could not be confirmed; inspect settings before retrying.",
                "verified": False,
            }
        verified = await _section(course_id, ids[0])
        if verified is None or any(
            not _same(verified.get(key), value, key) for key, value in payload.items()
        ):
            return {
                "warning": "Section settings could not be verified by readback.",
                "verified": False,
            }
        return {
            "changed": True,
            "verified": True,
            "section": _shown(_pick(verified, _SECTION_KEYS)),
        }

    @mcp.tool(annotations=ToolAnnotations(destructive_hint=True, idempotent_hint=True))
    @validate_params
    async def delete_course_section(
        course_identifier: str | int,
        section_id: str | int,
        confirmation_token: str | None = None,
    ) -> dict[str, Any]:
        """Preview/confirm deleting an empty local section; refuse all remaining enrollments."""
        course_id, ids, error = await _context(
            course_identifier, section_id, educator=True
        )
        if error:
            return {"error": error}
        current = await _section(course_id, ids[0])
        if current is None:
            return {"error": "Could not verify the course section."}
        if any(
            current.get(key)
            for key in (
                "sis_section_id",
                "sis_import_id",
                "integration_id",
                "nonxlist_course_id",
            )
        ):
            return {
                "error": "SIS-managed and cross-listed section deletion is unsupported."
            }
        if current.get("total_students") != 0:
            return {
                "error": "Section deletion requires a verified student count of zero."
            }
        enrollments = await fetch_all_paginated_results(
            canvas_path("sections", ids[0], "enrollments"),
            {
                "per_page": 100,
                "state[]": [
                    "active",
                    "invited",
                    "creation_pending",
                    "completed",
                    "inactive",
                ],
            },
        )
        if not isinstance(enrollments, list) or enrollments:
            return {
                "error": "Section deletion requires a verified absence of remaining enrollments."
            }
        preview = _confirmation(
            "delete_course_section",
            course_id,
            _pick(current, _SECTION_KEYS),
            {"section_id": ids[0], "remaining_enrollments": 0},
            confirmation_token,
        )
        if preview:
            return preview
        response = await make_canvas_request("delete", canvas_path("sections", ids[0]))
        if (
            not isinstance(response, dict)
            or "error" in response
            or str(response.get("id")) != ids[0]
        ):
            return {
                "warning": "Section deletion could not be confirmed; inspect sections before retrying.",
                "deleted": False,
            }
        return {"deleted": True, "section_id": ids[0]}

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    @validate_params
    async def get_submission_details(
        course_identifier: str | int,
        assignment_id: str | int,
        student_id: str | int,
        include_history: bool = False,
        include_comments: bool = False,
        include_content: bool = False,
        history_limit: int = 20,
        comment_limit: int = 50,
    ) -> dict[str, Any]:
        """Read one student's submission status and optional history/comments through the central privacy gate.

        Content is opt-in; central anonymization can redact it. Attachment signed
        URLs, user profiles, graders and embedded course objects are omitted.
        """
        if not 1 <= history_limit <= 100 or not 1 <= comment_limit <= 100:
            return {
                "error": "history_limit and comment_limit must be between 1 and 100."
            }
        course_id, ids, error = await _context(
            course_identifier, assignment_id, student_id, educator=True
        )
        if error:
            return {"error": error}
        includes = []
        if include_history:
            includes.append("submission_history")
        if include_comments:
            includes.append("submission_comments")
        params = {"include[]": includes} if includes else None
        response = await make_canvas_request(
            "get",
            canvas_path(
                "courses", course_id, "assignments", ids[0], "submissions", ids[1]
            ),
            params=params,
        )
        if (
            not isinstance(response, dict)
            or "error" in response
            or str(response.get("assignment_id")) != ids[0]
            or str(response.get("user_id")) != ids[1]
        ):
            return {"error": "Could not verify the requested student submission."}
        result = _pick(response, _SUBMISSION_KEYS)
        if include_content:
            result.update(_pick(response, ("body", "url")))
            result["attachments"] = [
                _pick(item, ("id", "filename", "size", "content-type"))
                for item in (response.get("attachments") or [])
                if isinstance(item, dict)
            ]
        if include_history:
            history = [
                _pick(item, _SUBMISSION_KEYS)
                for item in (response.get("submission_history") or [])
                if isinstance(item, dict)
            ]
            result["submission_history"] = history[-history_limit:]
            result["history_count"] = len(history)
            result["history_truncated"] = len(history) > history_limit
        if include_comments:
            comments = [
                _pick(item, ("id", "author_id", "author_name", "comment", "created_at"))
                for item in (response.get("submission_comments") or [])
                if isinstance(item, dict)
            ]
            result["submission_comments"] = comments[-comment_limit:]
            result["comment_count"] = len(comments)
            result["comments_truncated"] = len(comments) > comment_limit
        return {"course_id": course_id, "submission": _shown(result)}

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    @validate_params
    async def get_course_late_policy(course_identifier: str | int) -> dict[str, Any]:
        """Read bounded course-wide late and missing submission deduction settings."""
        course_id, _ids, error = await _context(course_identifier, educator=True)
        if error:
            return {"error": error}
        response = await make_canvas_request(
            "get", canvas_path("courses", course_id, "late_policy")
        )
        policy = (
            response.get("late_policy")
            if isinstance(response, dict) and "error" not in response
            else None
        )
        if not isinstance(policy, dict):
            return {"error": "Could not read an existing course late policy."}
        return {
            "course_id": course_id,
            "late_policy": _pick(policy, ("id", "course_id") + _LATE_KEYS),
        }

    @mcp.tool(annotations=ToolAnnotations(destructive_hint=True, idempotent_hint=False))
    @validate_params
    async def create_course_late_policy(
        course_identifier: str | int,
        missing_submission_deduction_enabled: bool = False,
        missing_submission_deduction: float = 0,
        late_submission_deduction_enabled: bool = False,
        late_submission_deduction: float = 0,
        late_submission_interval: str = "day",
        late_submission_minimum_percent_enabled: bool = False,
        late_submission_minimum_percent: float = 0,
        confirmation_token: str | None = None,
    ) -> dict[str, Any]:
        """Preview/confirm the first course late policy only when Canvas reports none.

        Percentage fields must be finite and 0..100; interval is hour/day.
        Deductions default disabled. The new policy is verified by readback.
        """
        payload = {
            "missing_submission_deduction_enabled": missing_submission_deduction_enabled,
            "missing_submission_deduction": missing_submission_deduction,
            "late_submission_deduction_enabled": late_submission_deduction_enabled,
            "late_submission_deduction": late_submission_deduction,
            "late_submission_interval": late_submission_interval,
            "late_submission_minimum_percent_enabled": late_submission_minimum_percent_enabled,
            "late_submission_minimum_percent": late_submission_minimum_percent,
        }
        if any(
            not math.isfinite(value) or not 0 <= value <= 100
            for value in (
                missing_submission_deduction,
                late_submission_deduction,
                late_submission_minimum_percent,
            )
        ) or late_submission_interval not in {"hour", "day"}:
            return {
                "error": "Policy percentages must be finite and 0..100; interval must be hour or day."
            }
        course_id, _ids, error = await _context(course_identifier, educator=True)
        if error:
            return {"error": error}
        endpoint = canvas_path("courses", course_id, "late_policy")
        current = await make_canvas_request("get", endpoint)
        if (
            not isinstance(current, dict)
            or "error" in current
            or "late_policy" not in current
            or current["late_policy"] is not None
        ):
            return {
                "error": "Canvas must explicitly report no existing late policy before creation."
            }
        preview = _confirmation(
            "create_course_late_policy",
            course_id,
            {"late_policy": None},
            payload,
            confirmation_token,
        )
        if preview:
            return preview
        return await _write_verified("post", endpoint, payload, "late_policy", endpoint)

    @mcp.tool(annotations=ToolAnnotations(destructive_hint=True, idempotent_hint=True))
    @validate_params
    async def update_course_late_policy(
        course_identifier: str | int,
        missing_submission_deduction_enabled: bool | None = None,
        missing_submission_deduction: float | None = None,
        late_submission_deduction_enabled: bool | None = None,
        late_submission_deduction: float | None = None,
        late_submission_interval: str | None = None,
        late_submission_minimum_percent_enabled: bool | None = None,
        late_submission_minimum_percent: float | None = None,
        confirmation_token: str | None = None,
    ) -> dict[str, Any]:
        """Preview/confirm course-wide deductions; percentages must be 0..100 and interval hour/day.

        Changes can affect existing student grades. Only existing policies are
        patched; omitted fields are preserved and every write is read back.
        """
        values = {
            "missing_submission_deduction_enabled": missing_submission_deduction_enabled,
            "missing_submission_deduction": missing_submission_deduction,
            "late_submission_deduction_enabled": late_submission_deduction_enabled,
            "late_submission_deduction": late_submission_deduction,
            "late_submission_interval": late_submission_interval,
            "late_submission_minimum_percent_enabled": late_submission_minimum_percent_enabled,
            "late_submission_minimum_percent": late_submission_minimum_percent,
        }
        payload = {key: value for key, value in values.items() if value is not None}
        if not payload:
            return {"error": "No late-policy settings were supplied."}
        for key in (
            "missing_submission_deduction",
            "late_submission_deduction",
            "late_submission_minimum_percent",
        ):
            value = payload.get(key)
            if value is not None and (
                not isinstance(value, (float, int))
                or not math.isfinite(value)
                or not 0 <= value <= 100
            ):
                return {"error": f"{key} must be a finite percentage from 0 to 100."}
        if late_submission_interval is not None and late_submission_interval not in {
            "hour",
            "day",
        }:
            return {"error": "late_submission_interval must be hour or day."}
        course_id, _ids, error = await _context(course_identifier, educator=True)
        if error:
            return {"error": error}
        endpoint = canvas_path("courses", course_id, "late_policy")
        response = await make_canvas_request("get", endpoint)
        current = (
            response.get("late_policy")
            if isinstance(response, dict) and "error" not in response
            else None
        )
        if not isinstance(current, dict):
            return {
                "error": "Could not read an existing course late policy; no policy was created."
            }
        preview = _confirmation(
            "update_course_late_policy",
            course_id,
            _pick(current, ("id", "course_id") + _LATE_KEYS),
            payload,
            confirmation_token,
        )
        if preview:
            return preview
        return await _write_verified(
            "patch", endpoint, payload, "late_policy", endpoint
        )

    @mcp.tool(annotations=ToolAnnotations(destructive_hint=True, idempotent_hint=True))
    @validate_params
    async def unassign_peer_review(
        course_identifier: str | int,
        assignment_id: str | int,
        submission_id: str | int,
        reviewer_id: str | int,
        confirmation_token: str | None = None,
    ) -> dict[str, Any]:
        """Preview/confirm removal of an incomplete assigned peer review; preserve completed reviews."""
        course_id, ids, error = await _context(
            course_identifier, assignment_id, submission_id, reviewer_id, educator=True
        )
        if error:
            return {"error": error}
        endpoint = canvas_path(
            "courses",
            course_id,
            "assignments",
            ids[0],
            "submissions",
            ids[1],
            "peer_reviews",
        )
        reviews = await fetch_all_paginated_results(endpoint, {"per_page": 100})
        if not isinstance(reviews, list):
            return {"error": "Could not inspect assigned peer reviews."}
        matching = [
            item
            for item in reviews
            if isinstance(item, dict) and str(item.get("assessor_id")) == ids[2]
        ]
        if len(matching) != 1:
            return {"error": "Exactly one matching assigned peer review is required."}
        current = _pick(
            matching[0],
            (
                "id",
                "assessor_id",
                "asset_id",
                "asset_type",
                "workflow_state",
                "user_id",
            ),
        )
        preview = _confirmation(
            "unassign_peer_review",
            course_id,
            {"assignment_id": ids[0], "submission_id": ids[1], "review": current},
            {"reviewer_id": ids[2]},
            confirmation_token,
        )
        if current.get("workflow_state") != "assigned":
            return {
                "error": "Only incomplete assigned peer reviews can be unassigned; completed reviews are preserved."
            }
        if preview:
            return preview
        response = await make_canvas_request(
            "delete", endpoint, params={"user_id": ids[2]}
        )
        if isinstance(response, dict) and "error" in response:
            return {
                "warning": "Peer-review removal could not be confirmed; inspect assignments before retrying.",
                "verified": False,
            }
        remaining = await fetch_all_paginated_results(endpoint, {"per_page": 100})
        if not isinstance(remaining, list) or any(
            isinstance(item, dict) and str(item.get("assessor_id")) == ids[2]
            for item in remaining
        ):
            return {
                "warning": "Peer-review removal could not be verified by readback.",
                "verified": False,
            }
        return {
            "unassigned": True,
            "verified": True,
            "submission_id": ids[1],
            "reviewer_id": ids[2],
        }

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    @validate_params
    async def get_course_outcome_results(
        course_identifier: str | int,
        user_ids: list[int] | None = None,
        outcome_ids: list[int] | None = None,
        page: int = 1,
        per_page: int = 50,
    ) -> dict[str, Any]:
        """Read one bounded page of visible outcome scores, excluding linked student profiles.

        Optional ID filters accept up to 100 unique positive Canvas IDs.
        has_more/next_page reflect Canvas's next-page header. Hidden results,
        linked user objects and arbitrary include parameters are unavailable.
        """
        return await _outcome_page(
            course_identifier, "outcome_results", user_ids, outcome_ids, page, per_page
        )

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    @validate_params
    async def get_course_outcome_rollups(
        course_identifier: str | int,
        user_ids: list[int] | None = None,
        outcome_ids: list[int] | None = None,
        page: int = 1,
        per_page: int = 50,
        aggregate: str | None = None,
        aggregate_stat: str | None = None,
    ) -> dict[str, Any]:
        """Read one bounded outcome mastery rollup page or course mean/median aggregate.

        Linked profiles and student names are omitted. Scores are capped at
        100 per rollup with explicit truncation metadata. ID filters accept
        positive numeric IDs only.
        """
        return await _outcome_page(
            course_identifier,
            "outcome_rollups",
            user_ids,
            outcome_ids,
            page,
            per_page,
            aggregate,
            aggregate_stat,
        )
