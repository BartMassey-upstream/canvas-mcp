"""Student-data-free authoring tools for Canvas New Quizzes."""

import json
import math
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit, urlunsplit

import httpx
from fastmcp import FastMCP
from mcp.types import ToolAnnotations

from ..core.cache import get_course_code, get_course_id
from ..core.client import make_canvas_request
from ..core.config import get_config
from ..core.credentials import is_http_request_active
from ..core.file_validation import validate_file_for_upload
from ..core.path import canvas_path
from ..core.untrusted_content import (
    FENCE_LEAK_ERROR,
    contains_fence_markers,
    fence_untrusted,
    fence_untrusted_inline,
)
from ..core.validation import validate_params
from ..core.write_confirmation import (
    ConfirmationGuard,
    preview_with_token,
    redeem_confirmation,
)
from ..core.write_outcome import RequestFailure, WriteOutcome

_DELETE_NEW_QUIZ_GUARD = ConfirmationGuard(nothing_done="The New Quiz was not deleted.")
_DELETE_NEW_QUIZ_ITEM_GUARD = ConfirmationGuard(
    nothing_done="The New Quiz item was not deleted."
)

_GRADING_TYPES = {"pass_fail", "percent", "letter_grade", "gpa_scale", "points"}
_CALCULATOR_TYPES = {"none", "basic", "scientific"}
_QUESTION_TYPES = {
    "multi-answer",
    "matching",
    "categorization",
    "file-upload",
    "formula",
    "ordering",
    "rich-fill-blank",
    "hot-spot",
    "choice",
    "numeric",
    "true-false",
    "essay",
}
_MEDIA_MAX_BYTES = 20 * 1024 * 1024
_S3_MEDIA_HOST = re.compile(
    r"(?:[a-z0-9][a-z0-9.-]*\.)?s3(?:[.-][a-z0-9-]+)?\.amazonaws\.com(?:\.cn)?"
)
_QUIZ_SETTING_KEYS = {
    "calculator_type",
    "filter_ip_address",
    "filters",
    "one_at_a_time_type",
    "allow_backtracking",
    "shuffle_answers",
    "shuffle_questions",
    "require_student_access_code",
    "student_access_code",
    "has_time_limit",
    "session_time_limit_in_seconds",
    "multiple_attempts",
    "result_view_settings",
}
_MULTIPLE_ATTEMPT_KEYS = {
    "multiple_attempts_enabled",
    "attempt_limit",
    "max_attempts",
    "score_to_keep",
    "cooling_period",
    "cooling_period_seconds",
}
_RESULT_VIEW_KEYS = {
    "result_view_restricted",
    "display_points_awarded",
    "display_points_possible",
    "display_items",
    "display_item_response",
    "display_item_response_qualifier",
    "show_item_responses_at",
    "hide_item_responses_at",
    "display_item_response_correctness",
    "display_item_response_correctness_qualifier",
    "show_item_response_correctness_at",
    "hide_item_response_correctness_at",
    "display_item_correct_answer",
    "display_item_feedback",
}


def _json_text(value: object) -> str:
    return json.dumps(value, indent=2, sort_keys=True, default=str)


def _unsigned_media_url(value: object) -> str | None:
    if not isinstance(value, str) or any(char.isspace() for char in value):
        return None
    try:
        url = urlsplit(value)
        canvas = urlsplit(get_config().canvas_api_url)
        if (
            url.scheme != "https"
            or url.port not in {None, 443}
            or url.username is not None
            or url.password is not None
            or url.fragment
            or not url.hostname
            or not url.path.strip("/")
        ):
            return None
        same_canvas = (
            canvas.scheme == "https"
            and canvas.port in {None, 443}
            and url.hostname == canvas.hostname
        )
        if not same_canvas and not _S3_MEDIA_HOST.fullmatch(url.hostname):
            return None
        query = parse_qs(url.query)
        if not (query.get("X-Amz-Signature") or query.get("Signature")):
            return None
        return urlunsplit((url.scheme, url.netloc, url.path, "", ""))
    except ValueError:
        return None


def _format_new_quiz(quiz: dict[str, Any], *, include_instructions: bool) -> str:
    lines = [
        f"Backing Assignment ID: {quiz.get('id')}",
        "Title: "
        + fence_untrusted_inline(
            quiz.get("title") or "Untitled quiz", "New Quiz title"
        ),
        f"Assignment Group ID: {quiz.get('assignment_group_id', 'N/A')}",
        f"Points: {quiz.get('points_possible', 'N/A')}",
        f"Grading Type: {quiz.get('grading_type', 'N/A')}",
        f"Published: {quiz.get('published', False)}",
        f"Due: {quiz.get('due_at') or 'No due date'}",
        f"Unlock: {quiz.get('unlock_at') or 'No unlock date'}",
        f"Lock: {quiz.get('lock_at') or 'No lock date'}",
        "Settings:\n"
        + fence_untrusted(
            _json_text(quiz.get("quiz_settings") or {}), "New Quiz settings"
        ),
    ]
    if include_instructions:
        lines.append(
            "Instructions:\n"
            + fence_untrusted(quiz.get("instructions") or "", "New Quiz instructions")
        )
    return "\n".join(lines)


def _format_new_quiz_item(item: dict[str, Any]) -> str:
    raw_entry = item.get("entry")
    entry: dict[str, Any] = raw_entry if isinstance(raw_entry, dict) else {}
    title = entry.get("title") or "Untitled item"
    return "\n".join(
        [
            f"ID: {item.get('id')}",
            f"Position: {item.get('position', 'N/A')}",
            f"Points: {item.get('points_possible', 'N/A')}",
            f"Entry Type: {item.get('entry_type', 'N/A')}",
            f"Editable: {item.get('entry_editable', 'N/A')}",
            "Title: " + fence_untrusted_inline(title, "New Quiz item title"),
            "Item definition:\n"
            + fence_untrusted(_json_text(entry), "New Quiz item definition"),
        ]
    )


def _validate_new_quiz_settings(settings: dict[str, Any] | None) -> str | None:
    if settings is None:
        return None
    unknown = set(settings) - _QUIZ_SETTING_KEYS
    if unknown:
        return "Unsupported New Quiz setting(s): " + ", ".join(sorted(unknown))
    if contains_fence_markers(_json_text(settings)):
        return FENCE_LEAK_ERROR

    calculator = settings.get("calculator_type")
    if calculator is not None and calculator not in _CALCULATOR_TYPES:
        return "Invalid calculator_type. Use none, basic, or scientific."
    one_at_a_time = settings.get("one_at_a_time_type")
    if one_at_a_time is not None and one_at_a_time not in {"none", "question"}:
        return "Invalid one_at_a_time_type. Use none or question."
    if settings.get("allow_backtracking") is True and one_at_a_time == "none":
        return "Invalid allow_backtracking: one_at_a_time_type must be question."

    filters = settings.get("filters")
    if filters is not None:
        if not isinstance(filters, dict) or set(filters) != {"ips"}:
            return "Invalid filters. Use an object containing only an ips array."
        ips = filters.get("ips")
        if ips is not None and (
            not isinstance(ips, list)
            or any(
                not isinstance(pair, list)
                or len(pair) != 2
                or any(not isinstance(address, str) for address in pair)
                for pair in ips
            )
        ):
            return "Invalid filters.ips. Use pairs of start and end IP-address strings."

    attempts = settings.get("multiple_attempts")
    if attempts is not None:
        if not isinstance(attempts, dict):
            return "Invalid multiple_attempts: expected an object."
        unknown = set(attempts) - _MULTIPLE_ATTEMPT_KEYS
        if unknown:
            return "Unsupported multiple-attempt setting(s): " + ", ".join(
                sorted(unknown)
            )
        score_to_keep = attempts.get("score_to_keep")
        if score_to_keep is not None and score_to_keep not in {
            "average",
            "first",
            "highest",
            "latest",
        }:
            return "Invalid score_to_keep. Use average, first, highest, or latest."
        for key in ("max_attempts", "cooling_period_seconds"):
            value = attempts.get(key)
            if value is not None and (not isinstance(value, int) or value <= 0):
                return f"Invalid {key}: use a positive integer or null."
        if (
            attempts.get("attempt_limit") is True
            and attempts.get("multiple_attempts_enabled") is False
        ):
            return "Invalid attempt_limit: multiple_attempts_enabled cannot be false."
        if (
            attempts.get("cooling_period") is True
            and attempts.get("multiple_attempts_enabled") is False
        ):
            return "Invalid cooling_period: multiple_attempts_enabled cannot be false."

    result_view = settings.get("result_view_settings")
    if result_view is not None:
        if not isinstance(result_view, dict):
            return "Invalid result_view_settings: expected an object."
        unknown = set(result_view) - _RESULT_VIEW_KEYS
        if unknown:
            return "Unsupported result-view setting(s): " + ", ".join(sorted(unknown))
        response_qualifier = result_view.get("display_item_response_qualifier")
        if response_qualifier is not None and response_qualifier not in {
            "always",
            "once_per_attempt",
            "after_last_attempt",
            "once_after_last_attempt",
        }:
            return (
                "Invalid display_item_response_qualifier. Use always, "
                "once_per_attempt, after_last_attempt, or once_after_last_attempt."
            )
        correctness_qualifier = result_view.get(
            "display_item_response_correctness_qualifier"
        )
        if correctness_qualifier is not None and correctness_qualifier not in {
            "always",
            "after_last_attempt",
        }:
            return (
                "Invalid display_item_response_correctness_qualifier. "
                "Use always or after_last_attempt."
            )
    return None


def _new_quiz_payload(
    *,
    title: str | None = None,
    instructions: str | None = None,
    assignment_group_id: str | int | None = None,
    points_possible: float | None = None,
    due_at: str | None = None,
    lock_at: str | None = None,
    unlock_at: str | None = None,
    grading_type: str | None = None,
    quiz_settings: dict[str, Any] | None = None,
) -> dict[str, Any] | str:
    if any(
        contains_fence_markers(value)
        for value in (title, instructions)
        if value is not None
    ):
        return FENCE_LEAK_ERROR
    if points_possible is not None and (
        isinstance(points_possible, bool)
        or not math.isfinite(points_possible)
        or points_possible <= 0
    ):
        return "Invalid points_possible: use a positive number."
    if grading_type is not None and grading_type not in _GRADING_TYPES:
        return (
            "Invalid grading_type. Use pass_fail, percent, letter_grade, "
            "gpa_scale, or points."
        )
    settings_error = _validate_new_quiz_settings(quiz_settings)
    if settings_error:
        return settings_error
    values = {
        "title": title,
        "instructions": instructions,
        "assignment_group_id": assignment_group_id,
        "points_possible": points_possible,
        "due_at": due_at,
        "lock_at": lock_at,
        "unlock_at": unlock_at,
        "grading_type": grading_type,
        "quiz_settings": quiz_settings,
    }
    return {key: value for key, value in values.items() if value is not None}


def _question_item_payload(
    *,
    title: str | None = None,
    item_body: str | None = None,
    interaction_type_slug: str | None = None,
    scoring_algorithm: str | None = None,
    scoring_data: dict[str, Any] | None = None,
    points_possible: float | None = None,
    position: int | None = None,
    calculator_type: str | None = None,
    interaction_data: dict[str, Any] | None = None,
    properties: dict[str, Any] | None = None,
    feedback: dict[str, Any] | None = None,
    answer_feedback: dict[str, Any] | None = None,
    creating: bool = False,
) -> dict[str, Any] | str:
    if contains_fence_markers(
        _json_text(
            {
                "title": title,
                "item_body": item_body,
                "scoring_data": scoring_data,
                "interaction_data": interaction_data,
                "properties": properties,
                "feedback": feedback,
                "answer_feedback": answer_feedback,
            }
        )
    ):
        return FENCE_LEAK_ERROR
    if creating and not item_body:
        return "Invalid item_body: a question stem is required."
    if creating and not interaction_type_slug:
        return "Invalid interaction_type_slug: a question type is required."
    if (
        interaction_type_slug is not None
        and interaction_type_slug not in _QUESTION_TYPES
    ):
        return "Invalid interaction_type_slug. Use a documented New Quiz question type."
    if creating and not scoring_algorithm:
        return "Invalid scoring_algorithm: a scoring algorithm is required."
    if creating and scoring_data is None:
        return "Invalid scoring_data: scoring data is required."
    if creating and interaction_data is None:
        interaction_data = {}
    if points_possible is not None and (
        isinstance(points_possible, bool)
        or not math.isfinite(points_possible)
        or points_possible <= 0
    ):
        return "Invalid points_possible: use a positive number."
    if position is not None and (
        isinstance(position, bool) or not isinstance(position, int) or position <= 0
    ):
        return "Invalid position: use a positive integer."
    if calculator_type is not None and calculator_type not in _CALCULATOR_TYPES:
        return "Invalid calculator_type. Use none, basic, or scientific."

    entry_values = {
        "title": title,
        "item_body": item_body,
        "interaction_type_slug": interaction_type_slug,
        "scoring_algorithm": scoring_algorithm,
        "scoring_data": scoring_data,
        "calculator_type": calculator_type,
        "interaction_data": interaction_data,
        "properties": properties,
        "feedback": feedback,
        "answer_feedback": answer_feedback,
    }
    entry = {key: value for key, value in entry_values.items() if value is not None}
    item: dict[str, Any] = {"entry_type": "Item"}
    if points_possible is not None:
        item["points_possible"] = points_possible
    if position is not None:
        item["position"] = position
    if entry:
        item["entry"] = entry
    return item


def _definition_id(value: object) -> str | None:
    if isinstance(value, bool) or not isinstance(value, str | int):
        return None
    text = str(value)
    return text if text.isascii() and text.isdigit() and int(text) > 0 else None


def _definition_matches(expected: object, actual: object, key: str = "") -> bool:
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            field in actual and _definition_matches(value, actual[field], field)
            for field, value in expected.items()
        )
    if isinstance(expected, list):
        return (
            isinstance(actual, list)
            and len(expected) == len(actual)
            and all(
                _definition_matches(left, right)
                for left, right in zip(expected, actual, strict=True)
            )
        )
    if key.endswith("_at") and isinstance(expected, str) and isinstance(actual, str):
        try:
            return datetime.fromisoformat(
                expected.replace("Z", "+00:00")
            ) == datetime.fromisoformat(actual.replace("Z", "+00:00"))
        except ValueError:
            return expected == actual
    if key == "assignment_group_id":
        return _definition_id(expected) is not None and _definition_id(
            expected
        ) == _definition_id(actual)
    if isinstance(expected, bool) or isinstance(actual, bool):
        return type(expected) is type(actual) and expected == actual
    return expected == actual


def _definition_write_failure(response: dict[str, Any]) -> str:
    if isinstance(response, RequestFailure) and response.outcome in {
        WriteOutcome.REJECTED,
        WriteOutcome.NOT_DISPATCHED,
    }:
        return "Error: Canvas rejected the definition write or the request was not dispatched. No successful change was confirmed."
    return "Error: Write outcome unknown. Inspect the quiz before retrying; no automatic retry was made."


async def _verify_definition_write(
    response: object,
    payload: dict[str, Any],
    path: str,
    *,
    expected_id: str | int | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    recovery = " Inspect the quiz before retrying; no automatic retry was made."
    response_id = (
        _definition_id(response.get("id")) if isinstance(response, dict) else None
    )
    if response_id is None or (
        expected_id is not None and response_id != _definition_id(expected_id)
    ):
        return (
            None,
            "Error: Write outcome unknown: Canvas returned a missing, malformed, or different definition ID."
            + recovery,
        )
    if not isinstance(response, dict):
        return (
            None,
            "Error: Write outcome unknown: Canvas returned an invalid definition."
            + recovery,
        )
    if "entry_type" in payload and (
        response.get("entry_type") != "Item"
        or not isinstance(response.get("entry"), dict)
    ):
        return (
            None,
            "Error: Write outcome unknown: Canvas returned an invalid question definition."
            + recovery,
        )
    read_path = path if expected_id is not None else f"{path}/{response_id}"
    persisted = await make_canvas_request("get", read_path, api_root="quiz")
    if (
        not isinstance(persisted, dict)
        or "error" in persisted
        or _definition_id(persisted.get("id")) != response_id
    ):
        return (
            None,
            "Error: Write outcome unknown: an independent definition readback was unavailable or had a different ID."
            + recovery,
        )
    if not _definition_matches(payload, persisted):
        return (
            None,
            "Error: Write not verified: the persisted definition did not match all requested fields. Some changes may have been applied."
            + recovery,
        )
    return persisted, None


async def _assignment_work_state(
    course_id: str | int, assignment_id: str | int
) -> tuple[dict[str, Any] | None, str | None]:
    assignment = await make_canvas_request(
        "get", canvas_path("courses", course_id, "assignments", assignment_id)
    )
    if isinstance(assignment, dict) and "error" in assignment:
        return None, f"Error fetching backing assignment: {assignment['error']}"
    if not isinstance(assignment, dict):
        return None, "Error fetching backing assignment: invalid Canvas response"
    return assignment, None


def _has_student_work(assignment: dict[str, Any]) -> bool:
    needs_grading = assignment.get("needs_grading_count")
    return bool(assignment.get("has_submitted_submissions")) or (
        isinstance(needs_grading, int | float) and needs_grading > 0
    )


def _student_work_error(what: str) -> str:
    return (
        f"Error: this {what} has existing student work. It was not deleted. "
        "Pass allow_deleting_student_work=true only if deleting or invalidating "
        "student attempts, submissions, and grades is intentional."
    )


def register_new_quiz_tools(mcp: FastMCP) -> None:
    """Register New Quiz definition and QuestionItem authoring tools."""

    @mcp.tool(
        annotations=ToolAnnotations(destructive_hint=False, idempotent_hint=False)
    )
    @validate_params
    async def upload_new_quiz_media(
        course_identifier: str | int,
        assignment_id: str | int,
        file_path: str,
    ) -> dict[str, Any]:
        """Upload a local PNG/JPEG/GIF/WebP for a New Quiz hot-spot question.

        Local stdio only; maximum size 20 MiB. Accepts HTTPS Canvas or S3
        signed upload destinations. Returns an unsigned image_url for
        interaction_data, never the upload credential. Does not create an item.
        """
        if is_http_request_active():
            return {"error": "Local media uploads are only available over stdio."}
        file_path = str(Path(file_path).expanduser())
        validation = validate_file_for_upload(
            file_path,
            max_size_bytes=_MEDIA_MAX_BYTES,
            allowed_extensions={".png", ".jpg", ".jpeg", ".gif", ".webp"},
        )
        if not validation.valid:
            return {"error": validation.error}
        try:
            with Path(file_path).expanduser().open("rb") as source:
                content = source.read(_MEDIA_MAX_BYTES + 1)
        except OSError:
            return {"error": "Cannot read the local media file."}
        if not content or len(content) > _MEDIA_MAX_BYTES:
            return {"error": "Media must contain between 1 byte and 20 MiB."}
        course_id = await get_course_id(course_identifier)
        slot = await make_canvas_request(
            "get",
            canvas_path(
                "courses",
                course_id,
                "quizzes",
                assignment_id,
                "items",
                "media_upload_url",
            ),
            api_root="quiz",
        )
        if isinstance(slot, dict) and "error" in slot:
            return {"error": "Canvas could not provide a media upload destination."}
        upload_url = slot.get("url") if isinstance(slot, dict) else None
        image_url = _unsigned_media_url(upload_url)
        if image_url is None or not isinstance(upload_url, str):
            return {
                "error": "Canvas returned an unsupported or invalid media upload destination. No file was sent."
            }
        try:
            async with httpx.AsyncClient(
                timeout=get_config().api_timeout,
                follow_redirects=False,
                trust_env=False,
            ) as storage:
                response = await storage.put(
                    upload_url,
                    content=content,
                    headers={"Content-Type": validation.mime_type},
                )
        except httpx.HTTPError:
            return {
                "error": "Media upload outcome is unknown. Inspect the quiz before retrying; no automatic retry was made."
            }
        if response.status_code not in {200, 201, 204}:
            return {
                "error": f"Media upload was not confirmed (HTTP {response.status_code}); redirects are not followed. No automatic retry was made."
            }
        return {
            "course_id": str(course_id),
            "assignment_id": str(assignment_id),
            "image_url": image_url,
            "size_bytes": len(content),
            "content_type": validation.mime_type,
        }

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    @validate_params
    async def list_new_quizzes(course_identifier: str | int) -> str:
        """List New Quiz definitions, never attempts, responses, or reports."""
        course_id = await get_course_id(course_identifier)
        quizzes = await make_canvas_request(
            "get", canvas_path("courses", course_id, "quizzes"), api_root="quiz"
        )
        if isinstance(quizzes, dict) and "error" in quizzes:
            return f"Error listing New Quizzes: {quizzes['error']}"
        if not isinstance(quizzes, list):
            return "Error listing New Quizzes: invalid Canvas response"
        if not quizzes:
            return f"No New Quizzes found for course {course_identifier}."
        course_display = await get_course_code(course_id) or course_identifier
        return f"New Quizzes for course {course_display}:\n\n" + "\n\n".join(
            _format_new_quiz(quiz, include_instructions=False) for quiz in quizzes
        )

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    @validate_params
    async def get_new_quiz(
        course_identifier: str | int, assignment_id: str | int
    ) -> str:
        """Get a New Quiz by its backing assignment ID without student activity."""
        course_id = await get_course_id(course_identifier)
        quiz = await make_canvas_request(
            "get",
            canvas_path("courses", course_id, "quizzes", assignment_id),
            api_root="quiz",
        )
        if isinstance(quiz, dict) and "error" in quiz:
            return f"Error fetching New Quiz: {quiz['error']}"
        if not isinstance(quiz, dict):
            return "Error fetching New Quiz: invalid Canvas response"
        return _format_new_quiz(quiz, include_instructions=True)

    @mcp.tool(
        annotations=ToolAnnotations(destructive_hint=False, idempotent_hint=False)
    )
    @validate_params
    async def create_new_quiz(
        course_identifier: str | int,
        title: str,
        instructions: str | None = None,
        assignment_group_id: str | int | None = None,
        points_possible: float | None = None,
        due_at: str | None = None,
        lock_at: str | None = None,
        unlock_at: str | None = None,
        grading_type: str = "points",
        quiz_settings: dict[str, Any] | None = None,
    ) -> str:
        """Create an unpublished New Quiz and its backing assignment.

        quiz_settings accepts every documented New Quizzes setting, including
        calculator_type, IP filters, question/answer shuffling, one-at-a-time,
        access code, time limit, multiple_attempts, and result_view_settings.
        Publish the returned assignment ID with update_assignment because the
        New Quizzes API does not document a writable published field.
        """
        payload = _new_quiz_payload(
            title=title,
            instructions=instructions,
            assignment_group_id=assignment_group_id,
            points_possible=points_possible,
            due_at=due_at,
            lock_at=lock_at,
            unlock_at=unlock_at,
            grading_type=grading_type,
            quiz_settings=quiz_settings,
        )
        if isinstance(payload, str):
            return payload
        course_id = await get_course_id(course_identifier)
        quiz = await make_canvas_request(
            "post",
            canvas_path("courses", course_id, "quizzes"),
            data={"quiz": payload},
            api_root="quiz",
        )
        if isinstance(quiz, dict) and "error" in quiz:
            return _definition_write_failure(quiz)
        quiz, verification_error = await _verify_definition_write(
            quiz,
            payload,
            canvas_path("courses", course_id, "quizzes"),
        )
        if verification_error:
            return verification_error
        assert quiz is not None
        return "New Quiz created:\n\n" + _format_new_quiz(
            quiz, include_instructions=False
        )

    @mcp.tool(annotations=ToolAnnotations(destructive_hint=True, idempotent_hint=True))
    @validate_params
    async def update_new_quiz(
        course_identifier: str | int,
        assignment_id: str | int,
        title: str | None = None,
        instructions: str | None = None,
        assignment_group_id: str | int | None = None,
        points_possible: float | None = None,
        due_at: str | None = None,
        lock_at: str | None = None,
        unlock_at: str | None = None,
        grading_type: str | None = None,
        quiz_settings: dict[str, Any] | None = None,
        clear_due_at: bool = False,
        clear_lock_at: bool = False,
        clear_unlock_at: bool = False,
    ) -> str:
        """Update a New Quiz by its backing assignment ID.

        quiz_settings is a partial nested settings object; JSON null clears a
        nullable nested value. Explicit clear flags clear the three top-level
        dates. Publish or unpublish with update_assignment.
        """
        for field_name, value, clear in (
            ("due_at", due_at, clear_due_at),
            ("lock_at", lock_at, clear_lock_at),
            ("unlock_at", unlock_at, clear_unlock_at),
        ):
            if value is not None and clear:
                return (
                    f"Invalid configuration: {field_name} and clear_{field_name} "
                    "cannot both be provided."
                )
        payload = _new_quiz_payload(
            title=title,
            instructions=instructions,
            assignment_group_id=assignment_group_id,
            points_possible=points_possible,
            due_at=due_at,
            lock_at=lock_at,
            unlock_at=unlock_at,
            grading_type=grading_type,
            quiz_settings=quiz_settings,
        )
        if isinstance(payload, str):
            return payload
        for field_name, clear in (
            ("due_at", clear_due_at),
            ("lock_at", clear_lock_at),
            ("unlock_at", clear_unlock_at),
        ):
            if clear:
                payload[field_name] = None
        if not payload:
            return "No New Quiz fields were provided to update."
        course_id = await get_course_id(course_identifier)
        quiz = await make_canvas_request(
            "patch",
            canvas_path("courses", course_id, "quizzes", assignment_id),
            data={"quiz": payload},
            api_root="quiz",
        )
        if isinstance(quiz, dict) and "error" in quiz:
            return _definition_write_failure(quiz)
        quiz, verification_error = await _verify_definition_write(
            quiz,
            payload,
            canvas_path("courses", course_id, "quizzes", assignment_id),
            expected_id=assignment_id,
        )
        if verification_error:
            return verification_error
        assert quiz is not None
        return "New Quiz updated:\n\n" + _format_new_quiz(
            quiz, include_instructions=False
        )

    @mcp.tool(annotations=ToolAnnotations(destructive_hint=True, idempotent_hint=True))
    @validate_params
    async def delete_new_quiz(
        course_identifier: str | int,
        assignment_id: str | int,
        allow_deleting_student_work: bool = False,
        confirmation_token: str | None = None,
    ) -> str:
        """Delete a New Quiz after student-work checks and confirmation."""
        course_id = await get_course_id(course_identifier)
        quiz = await make_canvas_request(
            "get",
            canvas_path("courses", course_id, "quizzes", assignment_id),
            api_root="quiz",
        )
        if isinstance(quiz, dict) and "error" in quiz:
            return f"Error fetching New Quiz: {quiz['error']}"
        if not isinstance(quiz, dict):
            return "Error fetching New Quiz: invalid Canvas response"
        assignment, assignment_error = await _assignment_work_state(
            course_id, assignment_id
        )
        if assignment_error:
            return assignment_error
        assert assignment is not None
        has_student_work = _has_student_work(assignment)
        if has_student_work and not allow_deleting_student_work:
            return _student_work_error("New Quiz")
        title = quiz.get("title") or "Untitled quiz"
        shown_title = fence_untrusted_inline(title, "New Quiz title")
        fingerprint = _DELETE_NEW_QUIZ_GUARD.fingerprint(
            "delete_new_quiz",
            str(course_id),
            str(assignment_id),
            title,
            str(quiz.get("points_possible")),
            str(quiz.get("published")),
            str(assignment.get("has_submitted_submissions")),
            str(assignment.get("needs_grading_count")),
            str(allow_deleting_student_work),
        )
        if not confirmation_token:
            preview = (
                f"Would delete New Quiz **{shown_title}** and backing assignment "
                f"{assignment_id}.\n"
                f"Published: {quiz.get('published', False)}\n"
                "Student-work deletion authorized: "
                f"{'yes' if allow_deleting_student_work else 'not needed'}\n"
                "Deleting it may remove associated attempts, submissions, and grades."
            )
            return preview_with_token(
                _DELETE_NEW_QUIZ_GUARD, fingerprint, "delete_new_quiz", preview
            )
        error = redeem_confirmation(
            _DELETE_NEW_QUIZ_GUARD, confirmation_token, fingerprint
        )
        if error:
            return error
        response = await make_canvas_request(
            "delete",
            canvas_path("courses", course_id, "quizzes", assignment_id),
            api_root="quiz",
        )
        if isinstance(response, dict) and "error" in response:
            return f"Error deleting New Quiz: {response['error']}"
        return f"New Quiz **{shown_title}** deleted."

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    @validate_params
    async def list_new_quiz_items(
        course_identifier: str | int, assignment_id: str | int
    ) -> str:
        """List a New Quiz's items without attempts or student responses."""
        course_id = await get_course_id(course_identifier)
        items = await make_canvas_request(
            "get",
            canvas_path("courses", course_id, "quizzes", assignment_id, "items"),
            api_root="quiz",
        )
        if isinstance(items, dict) and "error" in items:
            return f"Error listing New Quiz items: {items['error']}"
        if not isinstance(items, list):
            return "Error listing New Quiz items: invalid Canvas response"
        if not items:
            return f"No items found for New Quiz assignment {assignment_id}."
        return f"Items for New Quiz assignment {assignment_id}:\n\n" + "\n\n".join(
            _format_new_quiz_item(item) for item in items
        )

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    @validate_params
    async def get_new_quiz_item(
        course_identifier: str | int,
        assignment_id: str | int,
        item_id: str | int,
    ) -> str:
        """Get one New Quiz item definition without student responses."""
        course_id = await get_course_id(course_identifier)
        item = await make_canvas_request(
            "get",
            canvas_path(
                "courses", course_id, "quizzes", assignment_id, "items", item_id
            ),
            api_root="quiz",
        )
        if isinstance(item, dict) and "error" in item:
            return f"Error fetching New Quiz item: {item['error']}"
        if not isinstance(item, dict):
            return "Error fetching New Quiz item: invalid Canvas response"
        return _format_new_quiz_item(item)

    @mcp.tool(
        annotations=ToolAnnotations(destructive_hint=False, idempotent_hint=False)
    )
    @validate_params
    async def create_new_quiz_question(
        course_identifier: str | int,
        assignment_id: str | int,
        item_body: str,
        interaction_type_slug: str,
        scoring_algorithm: str,
        scoring_data: dict[str, Any],
        title: str | None = None,
        points_possible: float = 1,
        position: int | None = None,
        calculator_type: str = "none",
        interaction_data: dict[str, Any] | None = None,
        properties: dict[str, Any] | None = None,
        feedback: dict[str, Any] | None = None,
        answer_feedback: dict[str, Any] | None = None,
    ) -> str:
        """Create any API-writable QuestionItem in a New Quiz.

        The interaction, properties, and scoring objects use Canvas's documented
        per-question schemas. Choice-like types require UUIDs that identify
        choices. Stimulus and item-bank entries are read-only in Canvas's API.
        Hot-spot questions use image_url returned by upload_new_quiz_media.
        Omitted interaction_data defaults to an empty object for types such
        as numeric and formula; supply the documented data for other types.
        """
        payload = _question_item_payload(
            title=title,
            item_body=item_body,
            interaction_type_slug=interaction_type_slug,
            scoring_algorithm=scoring_algorithm,
            scoring_data=scoring_data,
            points_possible=points_possible,
            position=position,
            calculator_type=calculator_type,
            interaction_data=interaction_data,
            properties=properties,
            feedback=feedback,
            answer_feedback=answer_feedback,
            creating=True,
        )
        if isinstance(payload, str):
            return payload
        course_id = await get_course_id(course_identifier)
        item = await make_canvas_request(
            "post",
            canvas_path("courses", course_id, "quizzes", assignment_id, "items"),
            data={"item": payload},
            api_root="quiz",
        )
        if isinstance(item, dict) and "error" in item:
            return _definition_write_failure(item)
        item, verification_error = await _verify_definition_write(
            item,
            payload,
            canvas_path("courses", course_id, "quizzes", assignment_id, "items"),
        )
        if verification_error:
            return verification_error
        assert item is not None
        return "New Quiz question created:\n\n" + _format_new_quiz_item(item)

    @mcp.tool(annotations=ToolAnnotations(destructive_hint=True, idempotent_hint=True))
    @validate_params
    async def update_new_quiz_question(
        course_identifier: str | int,
        assignment_id: str | int,
        item_id: str | int,
        title: str | None = None,
        item_body: str | None = None,
        interaction_type_slug: str | None = None,
        scoring_algorithm: str | None = None,
        scoring_data: dict[str, Any] | None = None,
        points_possible: float | None = None,
        position: int | None = None,
        calculator_type: str | None = None,
        interaction_data: dict[str, Any] | None = None,
        properties: dict[str, Any] | None = None,
        feedback: dict[str, Any] | None = None,
        answer_feedback: dict[str, Any] | None = None,
    ) -> str:
        """Update an API-writable QuestionItem in a New Quiz."""
        payload = _question_item_payload(
            title=title,
            item_body=item_body,
            interaction_type_slug=interaction_type_slug,
            scoring_algorithm=scoring_algorithm,
            scoring_data=scoring_data,
            points_possible=points_possible,
            position=position,
            calculator_type=calculator_type,
            interaction_data=interaction_data,
            properties=properties,
            feedback=feedback,
            answer_feedback=answer_feedback,
        )
        if isinstance(payload, str):
            return payload
        if set(payload) == {"entry_type"}:
            return "No New Quiz question fields were provided to update."
        course_id = await get_course_id(course_identifier)
        item = await make_canvas_request(
            "patch",
            canvas_path(
                "courses", course_id, "quizzes", assignment_id, "items", item_id
            ),
            data={"item": payload},
            api_root="quiz",
        )
        if isinstance(item, dict) and "error" in item:
            return _definition_write_failure(item)
        item, verification_error = await _verify_definition_write(
            item,
            payload,
            canvas_path(
                "courses", course_id, "quizzes", assignment_id, "items", item_id
            ),
            expected_id=item_id,
        )
        if verification_error:
            return verification_error
        assert item is not None
        return "New Quiz question updated:\n\n" + _format_new_quiz_item(item)

    @mcp.tool(annotations=ToolAnnotations(destructive_hint=True, idempotent_hint=True))
    @validate_params
    async def delete_new_quiz_item(
        course_identifier: str | int,
        assignment_id: str | int,
        item_id: str | int,
        allow_deleting_student_work: bool = False,
        confirmation_token: str | None = None,
    ) -> str:
        """Delete a New Quiz item after student-work checks and confirmation."""
        course_id = await get_course_id(course_identifier)
        item = await make_canvas_request(
            "get",
            canvas_path(
                "courses", course_id, "quizzes", assignment_id, "items", item_id
            ),
            api_root="quiz",
        )
        if isinstance(item, dict) and "error" in item:
            return f"Error fetching New Quiz item: {item['error']}"
        if not isinstance(item, dict):
            return "Error fetching New Quiz item: invalid Canvas response"
        assignment, assignment_error = await _assignment_work_state(
            course_id, assignment_id
        )
        if assignment_error:
            return assignment_error
        assert assignment is not None
        has_student_work = _has_student_work(assignment)
        if has_student_work and not allow_deleting_student_work:
            return _student_work_error("New Quiz")
        raw_entry = item.get("entry")
        entry: dict[str, Any] = raw_entry if isinstance(raw_entry, dict) else {}
        title = entry.get("title") or "Untitled item"
        shown_title = fence_untrusted_inline(title, "New Quiz item title")
        fingerprint = _DELETE_NEW_QUIZ_ITEM_GUARD.fingerprint(
            "delete_new_quiz_item",
            str(course_id),
            str(assignment_id),
            str(item_id),
            title,
            str(item.get("position")),
            str(item.get("points_possible")),
            _json_text(entry),
            str(assignment.get("has_submitted_submissions")),
            str(assignment.get("needs_grading_count")),
            str(allow_deleting_student_work),
        )
        if not confirmation_token:
            preview = (
                f"Would delete New Quiz item **{shown_title}** (ID: {item_id}) "
                f"from backing assignment {assignment_id}.\n"
                "Student-work deletion authorized: "
                f"{'yes' if allow_deleting_student_work else 'not needed'}\n"
                "Deleting it may invalidate existing attempts and grades."
            )
            return preview_with_token(
                _DELETE_NEW_QUIZ_ITEM_GUARD,
                fingerprint,
                "delete_new_quiz_item",
                preview,
            )
        error = redeem_confirmation(
            _DELETE_NEW_QUIZ_ITEM_GUARD, confirmation_token, fingerprint
        )
        if error:
            return error
        response = await make_canvas_request(
            "delete",
            canvas_path(
                "courses", course_id, "quizzes", assignment_id, "items", item_id
            ),
            api_root="quiz",
        )
        if isinstance(response, dict) and "error" in response:
            return f"Error deleting New Quiz item: {response['error']}"
        return f"New Quiz item **{shown_title}** deleted."
