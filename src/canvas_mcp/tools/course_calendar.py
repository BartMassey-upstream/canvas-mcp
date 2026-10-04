"""Confirmed authoring of ordinary single-context course calendar events."""

import datetime
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
from ..core.validation import validate_params
from ..core.write_confirmation import (
    ConfirmationGuard,
    preview_with_token,
    redeem_confirmation,
)

_GUARDS = {
    name: ConfirmationGuard(nothing_done="The calendar was not changed.")
    for name in (
        "create_course_calendar_event",
        "update_course_calendar_event",
        "delete_course_calendar_event",
    )
}
_TEXT_FIELDS = ("title", "description", "location_name", "location_address")
_EVENT_FIELDS = (
    "id",
    "type",
    "context_code",
    "all_context_codes",
    "start_at",
    "end_at",
    "all_day",
    "all_day_date",
    "workflow_state",
    "created_at",
    "updated_at",
    "parent_event_id",
    "child_events_count",
    "hidden",
    "series_uuid",
    "rrule",
    "appointment_group_id",
    "effective_context_code",
)


def _positive(value: Any) -> bool:
    return type(value) is int and value > 0


async def _course(identifier: str | int) -> str:
    course_id: Any = await get_course_id(identifier)
    if isinstance(course_id, bool):
        raise ValueError("Canvas did not establish a valid course ID.")
    if (
        not str(course_id).isascii()
        or not str(course_id).isdigit()
        or int(course_id) <= 0
    ):
        course = await make_canvas_request("get", canvas_path("courses", course_id))
        if (
            not isinstance(course, dict)
            or "error" in course
            or not _positive(course.get("id"))
        ):
            raise ValueError("Canvas did not establish a canonical course ID.")
        course_id = course["id"]
    return str(int(course_id))


async def _permission(course_id: str) -> bool:
    response = await make_canvas_request(
        "get",
        canvas_path("courses", course_id, "permissions"),
        params={"permissions[]": ["manage_calendar"]},
    )
    return (
        isinstance(response, dict)
        and "error" not in response
        and response.get("manage_calendar") is True
    )


def _ordinary(raw: Any, course_id: str, deleted: bool = False) -> bool:
    if not isinstance(raw, dict) or "error" in raw or not _positive(raw.get("id")):
        return False
    for field in (*_TEXT_FIELDS, "all_day_date", "created_at", "updated_at"):
        if raw.get(field) is not None and not isinstance(raw[field], str):
            return False
    if not isinstance(raw.get("title"), str) or type(raw.get("all_day")) is not bool:
        return False
    for field in ("start_at", "end_at"):
        value = raw.get(field)
        if value is not None:
            if not isinstance(value, str):
                return False
            try:
                _timestamp(value)
            except ValueError:
                return False
    context = "course_" + course_id
    return (
        raw.get("type") == "event"
        and raw.get("context_code") == context
        and raw.get("all_context_codes") == context
        and raw.get("effective_context_code") in (None, context)
        and raw.get("workflow_state") == ("deleted" if deleted else "active")
        and raw.get("parent_event_id") is None
        and "parent_event_id" in raw
        and type(raw.get("child_events_count")) is int
        and raw["child_events_count"] == 0
        and raw.get("hidden") is False
        and not raw.get("child_events")
        and not any(
            raw.get(key)
            for key in (
                "appointment_group_id",
                "appointment_group_url",
                "reserve_url",
                "series_uuid",
                "rrule",
                "series_head",
                "web_conference",
                "user",
                "group",
            )
        )
        and "series_uuid" in raw
        and "rrule" in raw
    )


def _snapshot(raw: dict[str, Any]) -> dict[str, Any]:
    return {field: raw.get(field) for field in (*_EVENT_FIELDS, *_TEXT_FIELDS)}


def _event_view(raw: dict[str, Any]) -> dict[str, Any]:
    result = {
        field: raw.get(field)
        for field in (
            "id",
            "context_code",
            "start_at",
            "end_at",
            "all_day",
            "all_day_date",
            "workflow_state",
            "updated_at",
        )
    }
    for field in _TEXT_FIELDS:
        if isinstance(raw.get(field), str):
            result[field] = fence_untrusted(raw[field], "calendar event " + field)
    return result


async def _event(course_id: str, event_id: int) -> dict[str, Any] | None:
    raw = await make_canvas_request("get", canvas_path("calendar_events", event_id))
    return raw if _ordinary(raw, course_id) and raw["id"] == event_id else None


def _timestamp(value: str) -> datetime.datetime:
    try:
        result = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(
            "Dates must be ISO 8601 timestamps with explicit timezones."
        ) from exc
    if result.tzinfo is None:
        raise ValueError("Dates must include an explicit timezone.")
    return result


def _payload(
    title: str | None,
    description: str | None,
    start_at: str | None,
    end_at: str | None,
    location_name: str | None,
    location_address: str | None,
    all_day: bool | None,
) -> dict[str, Any]:
    values: dict[str, Any] = {
        "title": title,
        "description": description,
        "start_at": start_at,
        "end_at": end_at,
        "location_name": location_name,
        "location_address": location_address,
        "all_day": all_day,
    }
    if any(
        contains_fence_markers(values[field])
        for field in _TEXT_FIELDS
        if values[field] is not None
    ):
        raise ValueError(FENCE_LEAK_ERROR)
    if title is not None and not title.strip():
        raise ValueError("title cannot be blank.")
    for field in ("start_at", "end_at"):
        if values[field] is not None:
            values[field] = _timestamp(values[field]).isoformat()
    payload = {key: value for key, value in values.items() if value is not None}
    _date_order(payload, require_all_day_start=False)
    return payload


def _date_order(values: dict[str, Any], require_all_day_start: bool = True) -> None:
    start = _timestamp(values["start_at"]) if values.get("start_at") else None
    end = _timestamp(values["end_at"]) if values.get("end_at") else None
    if start is not None and end is not None and end < start:
        raise ValueError("end_at cannot be before start_at.")
    if values.get("all_day") is True:
        if (require_all_day_start and start is None) or any(
            date is not None
            and (date.hour or date.minute or date.second or date.microsecond)
            for date in (start, end)
        ):
            raise ValueError(
                "All-day events require midnight timestamps and a start date."
            )


def _echoes(raw: dict[str, Any], payload: dict[str, Any]) -> bool:
    for key, expected in payload.items():
        actual = raw.get(key)
        if key in ("start_at", "end_at"):
            if not isinstance(actual, str):
                return False
            try:
                if _timestamp(actual) != _timestamp(expected):
                    return False
            except ValueError:
                return False
        elif actual != expected:
            return False
    return True


async def _change(
    name: str,
    course_id: str,
    payload: dict[str, Any],
    current: dict[str, Any] | None,
    confirmation_token: str | None,
) -> str:
    if not await _permission(course_id):
        return "Error: Canvas did not confirm manage_calendar permission. The calendar was not changed."
    fingerprint = _GUARDS[name].fingerprint(
        name,
        course_id,
        json.dumps(payload, sort_keys=True),
        json.dumps(_snapshot(current) if current else {}, sort_keys=True),
    )
    if not confirmation_token:
        preview = fence_untrusted(
            json.dumps(
                {
                    "course_id": course_id,
                    "current": _snapshot(current) if current else None,
                    "requested": payload,
                },
                sort_keys=True,
                indent=2,
            ),
            "course calendar change preview",
        )
        return preview_with_token(
            _GUARDS[name],
            fingerprint,
            name,
            preview,
            action=(
                "delete"
                if name == "delete_course_calendar_event"
                else "apply this calendar change"
            ),
        )
    error = redeem_confirmation(_GUARDS[name], confirmation_token, fingerprint)
    if error:
        return error
    if not await _permission(course_id):
        return (
            "Error: manage_calendar permission changed. The calendar was not changed."
        )
    method = (
        "post"
        if current is None
        else "delete" if name == "delete_course_calendar_event" else "put"
    )
    path = (
        canvas_path("calendar_events")
        if current is None
        else canvas_path("calendar_events", current["id"])
    )
    try:
        response = await make_canvas_request(
            method, path, data={"calendar_event": payload} if payload else None
        )
    except Exception:
        return "Warning: the calendar write outcome is unknown. Read the calendar before retrying."
    deleted = method == "delete"
    if not _ordinary(response, course_id, deleted=deleted) or (
        current and response["id"] != current["id"]
    ):
        return "Warning: Canvas did not confirm the course calendar write. Read the calendar before retrying."
    if deleted:
        return f"Course calendar event {response['id']} deleted."
    verified = await _event(course_id, response["id"])
    if verified is None or not _echoes(verified, payload):
        return "Warning: Canvas accepted the write but did not verify the requested fields. Read the calendar before retrying."
    return f"Course calendar event {response['id']} {'created' if method == 'post' else 'updated'}."


def register_course_calendar_tools(mcp: FastMCP) -> None:
    """Register educator/all course event tools; no creator or personal writes."""

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    @validate_params
    async def list_course_calendar_events(
        course_identifier: str | int,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> dict[str, Any]:
        """List ordinary single-course events; default is all dated/undated events.

        Optional bounds use YYYY-MM-DD. Appointment slots, sections, recurring
        series, shared calendars, and assignment due-date events are excluded.
        """
        try:
            parsed = {}
            for key, value in (("start_date", start_date), ("end_date", end_date)):
                if value is not None:
                    if len(value) != 10:
                        raise ValueError("Date bounds must use YYYY-MM-DD.")
                    parsed[key] = datetime.date.fromisoformat(value)
            if len(parsed) == 2 and parsed["end_date"] < parsed["start_date"]:
                raise ValueError("end_date cannot be before start_date.")
            course_id = await _course(course_identifier)
            params: dict[str, Any] = {
                "type": "event",
                "context_codes[]": ["course_" + course_id],
                "excludes[]": ["child_events", "assignment"],
                "per_page": 100,
                "all_events": not parsed,
            }
            params.update({key: value.isoformat() for key, value in parsed.items()})
            raw = await fetch_all_paginated_results(
                canvas_path("calendar_events"), params=params
            )
            if not isinstance(raw, list) or any(
                not isinstance(item, dict) or "error" in item for item in raw
            ):
                return {"error": "Canvas did not return a complete event list."}
            events = [item for item in raw if _ordinary(item, course_id)]
            return {
                "course_id": course_id,
                "events": [_event_view(item) for item in events],
                "excluded_count": len(raw) - len(events),
            }
        except ValueError as exc:
            return {"error": str(exc)}

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    @validate_params
    async def get_course_calendar_event(
        course_identifier: str | int, event_id: int
    ) -> dict[str, Any]:
        """Read one ordinary course event; excludes reservation/participant data."""
        try:
            if not _positive(event_id):
                raise ValueError("event_id must be a positive numeric event ID.")
            event = await _event(await _course(course_identifier), event_id)
            if event is None:
                return {
                    "error": "Canvas did not verify an ordinary single-context event in this course."
                }
            return {"event": _event_view(event)}
        except ValueError as exc:
            return {"error": str(exc)}

    @mcp.tool(
        annotations=ToolAnnotations(destructive_hint=False, idempotent_hint=False)
    )
    @validate_params
    async def create_course_calendar_event(
        course_identifier: str | int,
        title: str,
        description: str | None = None,
        start_at: str | None = None,
        end_at: str | None = None,
        location_name: str | None = None,
        location_address: str | None = None,
        all_day: bool = False,
        confirmation_token: str | None = None,
    ) -> str:
        """Preview/confirm creating one course calendar event; dates require timezones.

        Omit dates for an undated event. All-day events use midnight timestamps.
        No appointment, recurrence, section, attendee or cross-calendar settings.
        """
        try:
            payload = _payload(
                title,
                description,
                start_at,
                end_at,
                location_name,
                location_address,
                all_day,
            )
            _date_order(payload)
            course_id = await _course(course_identifier)
            payload["context_code"] = "course_" + course_id
            return await _change(
                "create_course_calendar_event",
                course_id,
                payload,
                None,
                confirmation_token,
            )
        except ValueError as exc:
            return "Error: " + str(exc)

    @mcp.tool(annotations=ToolAnnotations(destructive_hint=True, idempotent_hint=True))
    @validate_params
    async def update_course_calendar_event(
        course_identifier: str | int,
        event_id: int,
        title: str | None = None,
        description: str | None = None,
        start_at: str | None = None,
        end_at: str | None = None,
        location_name: str | None = None,
        location_address: str | None = None,
        all_day: bool | None = None,
        confirmation_token: str | None = None,
    ) -> str:
        """Preview/confirm changes to one ordinary course event, preserving omitted fields.

        Event/calendar context cannot be moved. No recurrence/series, appointment,
        section children, shared calendars or attendees. Dates need timezones.
        """
        try:
            if not _positive(event_id):
                raise ValueError("event_id must be a positive numeric event ID.")
            payload = _payload(
                title,
                description,
                start_at,
                end_at,
                location_name,
                location_address,
                all_day,
            )
            if not payload:
                raise ValueError("No calendar changes specified.")
            course_id = await _course(course_identifier)
            current = await _event(course_id, event_id)
            if current is None:
                raise ValueError(
                    "Canvas did not verify an ordinary single-context event in this course."
                )
            _date_order({**current, **payload})
            return await _change(
                "update_course_calendar_event",
                course_id,
                payload,
                current,
                confirmation_token,
            )
        except ValueError as exc:
            return "Error: " + str(exc)

    @mcp.tool(annotations=ToolAnnotations(destructive_hint=True, idempotent_hint=True))
    @validate_params
    async def delete_course_calendar_event(
        course_identifier: str | int,
        event_id: int,
        confirmation_token: str | None = None,
    ) -> str:
        """Preview/confirm deleting one ordinary single-context course event.

        Appointments, reservations, recurrence series, section children and
        multi-context events are refused. Requires actual manage_calendar rights.
        """
        try:
            if not _positive(event_id):
                raise ValueError("event_id must be a positive numeric event ID.")
            course_id = await _course(course_identifier)
            current = await _event(course_id, event_id)
            if current is None:
                raise ValueError(
                    "Canvas did not verify an ordinary single-context event in this course."
                )
            return await _change(
                "delete_course_calendar_event",
                course_id,
                {},
                current,
                confirmation_token,
            )
        except ValueError as exc:
            return "Error: " + str(exc)
