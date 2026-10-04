"""Classic Quiz authoring tools that do not read student activity."""

import json
import math
from typing import Any

from fastmcp import FastMCP
from mcp.types import ToolAnnotations

from ..core.cache import get_course_code, get_course_id
from ..core.client import fetch_all_paginated_results, make_canvas_request
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

_DELETE_GROUP_GUARD = ConfirmationGuard(nothing_done="The quiz group was not deleted.")

_DELETE_QUIZ_GUARD = ConfirmationGuard(nothing_done="The quiz was not deleted.")
_DELETE_QUESTION_GUARD = ConfirmationGuard(
    nothing_done="The quiz question was not deleted."
)


def _has_student_quiz_work(quiz: dict[str, Any]) -> bool:
    """Use Canvas's no-unpublish signal without fetching attempts or responses."""
    return quiz.get("published") is True and quiz.get("unpublishable") is False


def _student_work_delete_error(what: str) -> str:
    return (
        f"Error: this {what} has existing student work. It was not deleted. "
        "Pass allow_deleting_student_work=true only if deleting or invalidating "
        "student attempts and grades is intentional."
    )


def _format_quiz(quiz: dict[str, Any], *, include_description: bool = False) -> str:
    lines = [
        f"ID: {quiz.get('id')}",
        f"Title: {fence_untrusted_inline(quiz.get('title') or 'Untitled quiz', 'quiz title')}",
        f"Type: {quiz.get('quiz_type', 'N/A')}",
        f"Assignment Group ID: {quiz.get('assignment_group_id', 'N/A')}",
        f"Backing Assignment ID: {quiz.get('assignment_id', 'N/A')}",
        f"Points: {quiz.get('points_possible', 'N/A')}",
        f"Questions: {quiz.get('question_count', 'N/A')}",
        f"Published: {quiz.get('published', False)}",
        f"Time Limit: {quiz.get('time_limit') or 'None'}",
        f"Shuffle Answers: {quiz.get('shuffle_answers', False)}",
        f"Allowed Attempts: {quiz.get('allowed_attempts', 1)}",
        f"Scoring Policy: {quiz.get('scoring_policy') or 'N/A'}",
        f"Hide Results: {quiz.get('hide_results') or 'Never'}",
        f"Show Correct Answers: {quiz.get('show_correct_answers', True)}",
        "Show Correct Answers on Last Attempt Only: "
        f"{quiz.get('show_correct_answers_last_attempt', False)}",
        f"Show Correct Answers At: {quiz.get('show_correct_answers_at') or 'Immediately'}",
        f"Hide Correct Answers At: {quiz.get('hide_correct_answers_at') or 'Never'}",
        f"One-Time Results: {quiz.get('one_time_results', False)}",
        f"One Question at a Time: {quiz.get('one_question_at_a_time', False)}",
        f"Can't Go Back: {quiz.get('cant_go_back', False)}",
        "Access Code: "
        + fence_untrusted_inline(quiz.get("access_code") or "None", "quiz access code"),
        "IP Filter: "
        + fence_untrusted_inline(quiz.get("ip_filter") or "None", "quiz IP filter"),
        f"Only Visible to Overrides: {quiz.get('only_visible_to_overrides', False)}",
        f"Anonymous Survey Submissions: {quiz.get('anonymous_submissions', False)}",
        f"Due: {quiz.get('due_at') or 'No due date'}",
        f"Unlock: {quiz.get('unlock_at') or 'No unlock date'}",
        f"Lock: {quiz.get('lock_at') or 'No lock date'}",
    ]
    if include_description:
        lines.append(
            "Description:\n"
            + fence_untrusted(quiz.get("description") or "", "quiz description")
        )
    return "\n".join(lines)


def _format_question(question: dict[str, Any]) -> str:
    lines = [
            f"ID: {question.get('id')}",
            "Name: "
            + fence_untrusted_inline(
                question.get("question_name") or "Unnamed question",
                "quiz question name",
            ),
            f"Type: {question.get('question_type', 'N/A')}",
            f"Points: {question.get('points_possible', 'N/A')}",
            f"Position: {question.get('position', 'N/A')}",
            f"Quiz Group ID: {question.get('quiz_group_id')}",
            "Question text:\n"
            + fence_untrusted(
                question.get("question_text") or "", "quiz question text"
            ),
    ]
    for field in (
        "correct_comments", "incorrect_comments", "neutral_comments", "text_after_answers"
    ):
        if field in question:
            label = field.replace("_", " ")
            lines.append(
                f"{label.title()}:\n"
                + fence_untrusted(question[field] or "", f"quiz question {label}")
            )
    for field in ("formulas", "variables", "answer_tolerance"):
        if field in question:
            lines.append(field + ":\n" + fence_untrusted(
                json.dumps(question[field], default=str), "calculated question " + field
            ))
    if question.get("answers"):
        lines.append(
            "Answer definitions:\n"
            + fence_untrusted(
                json.dumps(question["answers"], indent=2, default=str),
                "quiz answer definitions",
            )
        )
    return "\n".join(lines)


def _quiz_payload(
    *,
    title: str | None = None,
    description: str | None = None,
    quiz_type: str | None = None,
    assignment_group_id: str | int | None = None,
    time_limit: int | None = None,
    shuffle_answers: bool | None = None,
    hide_results: str | None = None,
    show_correct_answers: bool | None = None,
    show_correct_answers_last_attempt: bool | None = None,
    show_correct_answers_at: str | None = None,
    hide_correct_answers_at: str | None = None,
    allowed_attempts: int | None = None,
    scoring_policy: str | None = None,
    one_question_at_a_time: bool | None = None,
    cant_go_back: bool | None = None,
    access_code: str | None = None,
    ip_filter: str | None = None,
    due_at: str | None = None,
    unlock_at: str | None = None,
    lock_at: str | None = None,
    published: bool | None = None,
    one_time_results: bool | None = None,
    only_visible_to_overrides: bool | None = None,
    anonymous_submissions: bool | None = None,
    creating: bool = False,
) -> dict[str, Any] | str:
    if any(
        contains_fence_markers(value)
        for value in (title, description, access_code, ip_filter)
        if value is not None
    ):
        return FENCE_LEAK_ERROR
    if quiz_type is not None and quiz_type not in {
        "practice_quiz",
        "assignment",
        "graded_survey",
        "survey",
    }:
        return "Invalid quiz_type. Use practice_quiz, assignment, graded_survey, or survey."
    if scoring_policy is not None and scoring_policy not in {
        "keep_highest",
        "keep_latest",
    }:
        return "Invalid scoring_policy. Use keep_highest or keep_latest."
    if hide_results is not None and hide_results not in {
        "always",
        "until_after_last_attempt",
    }:
        return "Invalid hide_results. Use always or until_after_last_attempt."
    multiple_attempts = allowed_attempts == -1 or (
        allowed_attempts is not None and allowed_attempts > 1
    )
    if allowed_attempts is not None and allowed_attempts < -1:
        return "Invalid allowed_attempts. Use -1 for unlimited or a non-negative integer."
    if scoring_policy is not None and (creating or allowed_attempts is not None) and not multiple_attempts:
        return "Invalid scoring_policy: it is only valid when allowed_attempts is -1 or greater than 1."
    if (
        hide_results == "until_after_last_attempt"
        and (creating or allowed_attempts is not None)
        and not multiple_attempts
    ):
        return "Invalid hide_results: until_after_last_attempt requires multiple attempts."
    if hide_results is not None and show_correct_answers is not None:
        return "Invalid result settings: show_correct_answers is only valid when hide_results is cleared."
    if show_correct_answers is False and any(
        value is not None
        for value in (
            show_correct_answers_last_attempt,
            show_correct_answers_at,
            hide_correct_answers_at,
        )
    ):
        return "Invalid result settings: correct-answer timing requires show_correct_answers=true."
    if (
        show_correct_answers_last_attempt is True
        and (creating or allowed_attempts is not None)
        and not multiple_attempts
    ):
        return "Invalid show_correct_answers_last_attempt: multiple attempts are required."
    if one_time_results is True and hide_results == "always":
        return "Invalid one_time_results: it cannot be used when hide_results is always."
    if cant_go_back is True and (
        one_question_at_a_time is False
        or (creating and one_question_at_a_time is None)
    ):
        return "Invalid cant_go_back: one_question_at_a_time must be true."
    if assignment_group_id is not None and quiz_type in {"practice_quiz", "survey"}:
        return "Invalid assignment_group_id: it is only valid for assignment or graded_survey quizzes."
    effective_quiz_type = quiz_type or ("assignment" if creating else None)
    if anonymous_submissions is not None and effective_quiz_type in {
        "practice_quiz",
        "assignment",
    }:
        return "Invalid anonymous_submissions: it is only valid for survey or graded_survey quizzes."

    values = {
        "title": title,
        "description": description,
        "quiz_type": quiz_type,
        "assignment_group_id": assignment_group_id,
        "time_limit": time_limit,
        "shuffle_answers": shuffle_answers,
        "hide_results": hide_results,
        "show_correct_answers": show_correct_answers,
        "show_correct_answers_last_attempt": show_correct_answers_last_attempt,
        "show_correct_answers_at": show_correct_answers_at,
        "hide_correct_answers_at": hide_correct_answers_at,
        "allowed_attempts": allowed_attempts,
        "scoring_policy": scoring_policy,
        "one_question_at_a_time": one_question_at_a_time,
        "cant_go_back": cant_go_back,
        "access_code": access_code,
        "ip_filter": ip_filter,
        "due_at": due_at,
        "unlock_at": unlock_at,
        "lock_at": lock_at,
        "published": published,
        "one_time_results": one_time_results,
        "only_visible_to_overrides": only_visible_to_overrides,
        "anonymous_submissions": anonymous_submissions,
    }
    return {key: value for key, value in values.items() if value is not None}



def _validate_calculated_definition(
    formulas: list[str] | None, variables: list[Any] | None,
    answer_tolerance: float | str | None, answers: list[Any] | None,
) -> str | None:
    if not formulas or not variables or not answers or answer_tolerance is None:
        return "Error: calculated questions require formulas, variables, answer_tolerance, and precomputed answers."
    if any(not isinstance(f, str) or not f.strip() or len(f) > 1024 for f in formulas):
        return "Error: calculated formulas must be nonempty strings of at most 1024 characters."
    try:
        tolerance = float(str(answer_tolerance).removesuffix("%"))
    except ValueError:
        return "Error: answer_tolerance must be numeric or a numeric percentage."
    if not math.isfinite(tolerance) or tolerance < 0:
        return "Error: answer_tolerance must be finite and nonnegative."
    bounds = {}
    for variable in variables:
        if not isinstance(variable, dict):
            return "Error: calculated variables must be objects."
        if set(variable) != {"name", "min", "max", "scale"}:
            return "Error: each calculated variable requires name, min, max, and scale."
        name = variable["name"]
        if not isinstance(name, str) or not name.strip() or len(name) > 1024 or name in bounds:
            return "Error: calculated variable names must be unique nonempty strings."
        if type(variable["scale"]) is not int or not 0 <= variable["scale"] <= 20:
            return "Error: variable scale must be an integer between 0 and 20."
        try:
            lower, upper = float(variable["min"]), float(variable["max"])
        except (TypeError, ValueError):
            return "Error: variable bounds must be numeric."
        if not math.isfinite(lower) or not math.isfinite(upper) or lower > upper:
            return "Error: variable bounds must be finite and min must not exceed max."
        bounds[name] = (lower, upper)
    for answer in answers:
        if not isinstance(answer, dict):
            return "Error: calculated answers must be objects."
        try:
            value = float(answer["answer_text"])
        except (KeyError, TypeError, ValueError):
            return "Error: calculated answers require numeric answer_text, not the read representation's answer key."
        if not math.isfinite(value):
            return "Error: calculated answer_text must be finite."
        values = answer.get("variables")
        if not isinstance(values, list) or len(values) != len(bounds):
            return "Error: every calculated answer must supply all declared variable values."
        seen = set()
        for variable in values:
            if not isinstance(variable, dict) or set(variable) != {"name", "value"}:
                return "Error: answer variables require name and value."
            name = variable["name"]
            if not isinstance(name, str) or name not in bounds or name in seen:
                return "Error: answer variable names must match the declarations exactly."
            seen.add(name)
            try:
                value = float(variable["value"])
            except (TypeError, ValueError):
                return "Error: answer variable values must be numeric."
            if not math.isfinite(value) or not bounds[name][0] <= value <= bounds[name][1]:
                return "Error: answer variable values must be finite and within declared bounds."
    return None


def _question_payload(
    *,
    question_name: str | None = None,
    question_text: str | None = None,
    question_type: str | None = None,
    points_possible: float | None = None,
    position: int | None = None,
    answers: list[dict[str, Any]] | None = None,
    quiz_group_id: int | None = None,
    correct_comments: str | None = None,
    incorrect_comments: str | None = None,
    neutral_comments: str | None = None,
    text_after_answers: str | None = None,
    formulas: list[str] | None = None,
    variables: list[dict[str, Any]] | None = None,
    answer_tolerance: float | str | None = None,
) -> dict[str, Any] | str:
    if any(
        contains_fence_markers(value)
        for value in (
            question_name, question_text, answers, correct_comments,
            incorrect_comments, neutral_comments, text_after_answers, formulas, variables,
        )
        if value is not None
    ):
        return FENCE_LEAK_ERROR
    if any(value is not None for value in (formulas, variables, answer_tolerance)):
        if question_type != "calculated_question":
            return "Error: calculated fields require explicit question_type=calculated_question."
        error = _validate_calculated_definition(formulas, variables, answer_tolerance, answers)
        if error:
            return error
    elif question_type == "calculated_question":
        return "Error: calculated questions require a complete calculated definition."
    if quiz_group_id is not None and quiz_group_id <= 0:
        return "Error: quiz_group_id must be a positive integer."
    values = {
        "question_name": question_name,
        "question_text": question_text,
        "question_type": question_type,
        "points_possible": points_possible,
        "position": position,
        "answers": answers,
        "quiz_group_id": quiz_group_id,
        "correct_comments": correct_comments,
        "incorrect_comments": incorrect_comments,
        "neutral_comments": neutral_comments,
        "text_after_answers": text_after_answers,
        "formulas": formulas,
        "variables": variables,
        "answer_tolerance": answer_tolerance,
    }
    return {key: value for key, value in values.items() if value is not None}



def _format_group(group: dict[str, Any]) -> str:
    return (f"ID: {group.get('id')}\n"
            + "Name: " + fence_untrusted_inline(group.get("name") or "Unnamed group", "quiz group name")
            + f"\nPick Count: {group.get('pick_count')}"
            + f"\nQuestion Points: {group.get('question_points')}"
            + f"\nAssessment Question Bank ID: {group.get('assessment_question_bank_id')}"
            + f"\nPosition: {group.get('position')}")


def _format_groups_response(response: Any) -> str:
    if not isinstance(response, dict):
        return "Error: Canvas returned an invalid quiz_groups response."
    if "error" in response:
        return f"Error accessing quiz groups: {response['error']}"
    groups = response.get("quiz_groups")
    if not isinstance(groups, list) or any(not isinstance(g, dict) for g in groups):
        return "Error: Canvas returned an invalid quiz_groups response."
    return "\n\n".join(_format_group(group) for group in groups) or "No quiz groups found."


def _group_payload(name: str | None, pick_count: int | None,
                   question_points: float | None) -> dict[str, Any] | str:
    if name is not None and contains_fence_markers(name):
        return FENCE_LEAK_ERROR
    if name is not None and not name.strip():
        return "Error: group name cannot be empty."
    if pick_count is not None and pick_count <= 0:
        return "Error: pick_count must be positive."
    if question_points is not None and (not math.isfinite(question_points) or question_points < 0):
        return "Error: question_points must be finite and nonnegative."
    return {key: value for key, value in {
        "name": name, "pick_count": pick_count, "question_points": question_points
    }.items() if value is not None}


def register_quiz_tools(mcp: FastMCP) -> None:
    """Register Classic Quiz definition and question authoring tools."""

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    @validate_params
    async def list_quizzes(
        course_identifier: str | int, search_term: str | None = None
    ) -> str:
        """List Classic Quizzes without submissions, attempts, or statistics."""
        course_id = await get_course_id(course_identifier)
        quizzes = await fetch_all_paginated_results(
            canvas_path('courses', course_id, 'quizzes'),
            {"per_page": 100, **({"search_term": search_term} if search_term else {})}
        )
        if isinstance(quizzes, dict) and "error" in quizzes:
            return f"Error listing quizzes: {quizzes['error']}"
        if not quizzes:
            return f"No Classic Quizzes found for course {course_identifier}."
        course_display = await get_course_code(course_id) or course_identifier
        return f"Classic Quizzes for course {course_display}:\n\n" + "\n\n".join(
            _format_quiz(quiz) for quiz in quizzes
        )

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    @validate_params
    async def get_quiz(course_identifier: str | int, quiz_id: str | int) -> str:
        """Get one Classic Quiz definition without student activity."""
        course_id = await get_course_id(course_identifier)
        quiz = await make_canvas_request(
            "get", canvas_path('courses', course_id, 'quizzes', quiz_id)
        )
        if "error" in quiz:
            return f"Error fetching quiz: {quiz['error']}"
        return _format_quiz(quiz, include_description=True)

    @mcp.tool(annotations=ToolAnnotations(destructive_hint=False, idempotent_hint=False))
    @validate_params
    async def create_quiz(
        course_identifier: str | int,
        title: str,
        description: str | None = None,
        quiz_type: str = "assignment",
        assignment_group_id: str | int | None = None,
        time_limit: int | None = None,
        shuffle_answers: bool | None = None,
        hide_results: str | None = None,
        show_correct_answers: bool | None = None,
        show_correct_answers_last_attempt: bool | None = None,
        show_correct_answers_at: str | None = None,
        hide_correct_answers_at: str | None = None,
        allowed_attempts: int | None = None,
        scoring_policy: str | None = None,
        one_question_at_a_time: bool | None = None,
        cant_go_back: bool | None = None,
        access_code: str | None = None,
        ip_filter: str | None = None,
        due_at: str | None = None,
        unlock_at: str | None = None,
        lock_at: str | None = None,
        published: bool = False,
        one_time_results: bool | None = None,
        only_visible_to_overrides: bool | None = None,
        anonymous_submissions: bool | None = None,
    ) -> str:
        """Create a Classic Quiz with every documented Canvas setting.

        Result visibility uses hide_results (always or
        until_after_last_attempt), show_correct_answers, the two correct-answer
        dates, and one_time_results. Set allowed_attempts=-1 for unlimited
        attempts. New quizzes default to unpublished.
        """
        payload = _quiz_payload(
            title=title,
            description=description,
            quiz_type=quiz_type,
            assignment_group_id=assignment_group_id,
            time_limit=time_limit,
            shuffle_answers=shuffle_answers,
            hide_results=hide_results,
            show_correct_answers=show_correct_answers,
            show_correct_answers_last_attempt=show_correct_answers_last_attempt,
            show_correct_answers_at=show_correct_answers_at,
            hide_correct_answers_at=hide_correct_answers_at,
            allowed_attempts=allowed_attempts,
            scoring_policy=scoring_policy,
            one_question_at_a_time=one_question_at_a_time,
            cant_go_back=cant_go_back,
            access_code=access_code,
            ip_filter=ip_filter,
            due_at=due_at,
            unlock_at=unlock_at,
            lock_at=lock_at,
            published=published,
            one_time_results=one_time_results,
            only_visible_to_overrides=only_visible_to_overrides,
            anonymous_submissions=anonymous_submissions,
            creating=True,
        )
        if isinstance(payload, str):
            return payload
        course_id = await get_course_id(course_identifier)
        quiz = await make_canvas_request(
            "post", canvas_path('courses', course_id, 'quizzes'), data={"quiz": payload}
        )
        if "error" in quiz:
            return f"Error creating quiz: {quiz['error']}"
        return "Classic Quiz created:\n\n" + _format_quiz(quiz)

    @mcp.tool(annotations=ToolAnnotations(destructive_hint=True, idempotent_hint=True))
    @validate_params
    async def update_quiz(
        course_identifier: str | int,
        quiz_id: str | int,
        title: str | None = None,
        description: str | None = None,
        quiz_type: str | None = None,
        assignment_group_id: str | int | None = None,
        time_limit: int | None = None,
        shuffle_answers: bool | None = None,
        hide_results: str | None = None,
        show_correct_answers: bool | None = None,
        show_correct_answers_last_attempt: bool | None = None,
        show_correct_answers_at: str | None = None,
        hide_correct_answers_at: str | None = None,
        allowed_attempts: int | None = None,
        scoring_policy: str | None = None,
        one_question_at_a_time: bool | None = None,
        cant_go_back: bool | None = None,
        access_code: str | None = None,
        ip_filter: str | None = None,
        due_at: str | None = None,
        unlock_at: str | None = None,
        lock_at: str | None = None,
        published: bool | None = None,
        one_time_results: bool | None = None,
        only_visible_to_overrides: bool | None = None,
        anonymous_submissions: bool | None = None,
        notify_of_update: bool | None = None,
        clear_time_limit: bool = False,
        clear_hide_results: bool = False,
        clear_show_correct_answers_at: bool = False,
        clear_hide_correct_answers_at: bool = False,
        clear_access_code: bool = False,
        clear_ip_filter: bool = False,
        clear_due_at: bool = False,
        clear_unlock_at: bool = False,
        clear_lock_at: bool = False,
    ) -> str:
        """Update every documented Classic Quiz setting.

        Nullable settings have explicit clear_* flags because an omitted None
        means "leave unchanged". notify_of_update is an update-time action, not
        a persisted quiz setting.
        """
        if time_limit is not None and clear_time_limit:
            return (
                "Invalid configuration: time_limit and clear_time_limit cannot "
                "both be provided."
            )

        date_updates = (
            ("show_correct_answers_at", show_correct_answers_at, clear_show_correct_answers_at),
            ("hide_correct_answers_at", hide_correct_answers_at, clear_hide_correct_answers_at),
            ("due_at", due_at, clear_due_at),
            ("unlock_at", unlock_at, clear_unlock_at),
            ("lock_at", lock_at, clear_lock_at),
        )
        for field_name, value, clear in date_updates:
            if value is not None and clear:
                return (
                    f"Invalid configuration: {field_name} and clear_{field_name} "
                    "cannot both be provided."
                )

        nullable_updates = (
            ("hide_results", hide_results, clear_hide_results),
            ("access_code", access_code, clear_access_code),
            ("ip_filter", ip_filter, clear_ip_filter),
        )
        for field_name, value, clear in nullable_updates:
            if value is not None and clear:
                return (
                    f"Invalid configuration: {field_name} and clear_{field_name} "
                    "cannot both be provided."
                )

        payload = _quiz_payload(
            title=title,
            description=description,
            quiz_type=quiz_type,
            assignment_group_id=assignment_group_id,
            time_limit=time_limit,
            shuffle_answers=shuffle_answers,
            hide_results=hide_results,
            show_correct_answers=show_correct_answers,
            show_correct_answers_last_attempt=show_correct_answers_last_attempt,
            show_correct_answers_at=show_correct_answers_at,
            hide_correct_answers_at=hide_correct_answers_at,
            allowed_attempts=allowed_attempts,
            scoring_policy=scoring_policy,
            one_question_at_a_time=one_question_at_a_time,
            cant_go_back=cant_go_back,
            access_code=access_code,
            ip_filter=ip_filter,
            due_at=due_at,
            unlock_at=unlock_at,
            lock_at=lock_at,
            published=published,
            one_time_results=one_time_results,
            only_visible_to_overrides=only_visible_to_overrides,
            anonymous_submissions=anonymous_submissions,
        )
        if isinstance(payload, str):
            return payload
        if clear_time_limit:
            payload["time_limit"] = None
        for field_name, _value, clear in date_updates:
            if clear:
                payload[field_name] = None
        for field_name, _value, clear in nullable_updates:
            if clear:
                payload[field_name] = None
        if notify_of_update is not None:
            payload["notify_of_update"] = notify_of_update
        if not payload:
            return "No quiz fields were provided to update."
        course_id = await get_course_id(course_identifier)
        quiz = await make_canvas_request(
            "put",
            canvas_path('courses', course_id, 'quizzes', quiz_id),
            data={"quiz": payload},
        )
        if "error" in quiz:
            return f"Error updating quiz: {quiz['error']}"
        return "Classic Quiz updated:\n\n" + _format_quiz(quiz)

    @mcp.tool(annotations=ToolAnnotations(destructive_hint=True, idempotent_hint=True))
    @validate_params
    async def delete_quiz(
        course_identifier: str | int,
        quiz_id: str | int,
        allow_deleting_student_work: bool = False,
        confirmation_token: str | None = None,
    ) -> str:
        """Delete a Classic Quiz after safety checks and confirmation."""
        course_id = await get_course_id(course_identifier)
        quiz = await make_canvas_request(
            "get", canvas_path('courses', course_id, 'quizzes', quiz_id)
        )
        if "error" in quiz:
            return f"Error fetching quiz: {quiz['error']}"
        has_student_work = _has_student_quiz_work(quiz)
        if has_student_work and not allow_deleting_student_work:
            return _student_work_delete_error("quiz")
        title = quiz.get("title") or "Untitled quiz"
        shown_title = fence_untrusted_inline(title, "quiz title")
        fingerprint = _DELETE_QUIZ_GUARD.fingerprint(
            "delete_quiz",
            str(course_id),
            str(quiz_id),
            title,
            str(quiz.get("question_count")),
            str(quiz.get("published")),
            str(quiz.get("unpublishable")),
            str(allow_deleting_student_work),
        )
        if not confirmation_token:
            preview = (
                f"Would delete Classic Quiz **{shown_title}** (ID: {quiz_id}).\n"
                f"Questions: {quiz.get('question_count', 'unknown')}\n"
                f"Published: {quiz.get('published', False)}\n"
                f"Student-work deletion authorized: "
                f"{'yes' if allow_deleting_student_work else 'not needed'}\n"
                "Deleting a graded quiz also removes its linked assignment and "
                "may remove associated submissions and grades."
            )
            return preview_with_token(
                _DELETE_QUIZ_GUARD, fingerprint, "delete_quiz", preview
            )
        error = redeem_confirmation(
            _DELETE_QUIZ_GUARD, confirmation_token, fingerprint
        )
        if error:
            return error
        response = await make_canvas_request(
            "delete", canvas_path('courses', course_id, 'quizzes', quiz_id)
        )
        if isinstance(response, dict) and "error" in response:
            return f"Error deleting quiz: {response['error']}"
        return f"Classic Quiz **{shown_title}** deleted."

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    @validate_params
    async def list_quiz_questions(
        course_identifier: str | int, quiz_id: str | int
    ) -> str:
        """List question definitions for a Classic Quiz, never responses."""
        course_id = await get_course_id(course_identifier)
        questions = await fetch_all_paginated_results(
            canvas_path('courses', course_id, 'quizzes', quiz_id, 'questions'),
            {"per_page": 100},
        )
        if isinstance(questions, dict) and "error" in questions:
            return f"Error listing quiz questions: {questions['error']}"
        if not questions:
            return f"No questions found for Classic Quiz {quiz_id}."
        return f"Questions for Classic Quiz {quiz_id}:\n\n" + "\n\n".join(
            _format_question(question) for question in questions
        )

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    @validate_params
    async def get_quiz_question(
        course_identifier: str | int,
        quiz_id: str | int,
        question_id: str | int,
    ) -> str:
        """Read one Classic Quiz question definition, never student responses."""
        course_id = await get_course_id(course_identifier)
        question = await make_canvas_request(
            "get",
            canvas_path("courses", course_id, "quizzes", quiz_id, "questions", question_id),
        )
        if not isinstance(question, dict):
            return "Error reading quiz question: invalid Canvas response."
        if "error" in question:
            return f"Error reading quiz question: {question['error']}"
        return _format_question(question)

    @mcp.tool(annotations=ToolAnnotations(destructive_hint=False, idempotent_hint=False))
    @validate_params
    async def create_quiz_question(
        course_identifier: str | int,
        quiz_id: str | int,
        question_name: str,
        question_text: str,
        question_type: str,
        points_possible: float = 1,
        position: int | None = None,
        answers: list[dict[str, Any]] | None = None,
        quiz_group_id: int | None = None,
        correct_comments: str | None = None,
        incorrect_comments: str | None = None,
        neutral_comments: str | None = None,
        text_after_answers: str | None = None,
        formulas: list[str] | None = None,
        variables: list[dict[str, Any]] | None = None,
        answer_tolerance: float | str | None = None,
    ) -> str:
        """Add a question definition to a Classic Quiz.

        Calculated questions require formulas (strings), variables with name,
        min/max and integer scale (0..20), answer_tolerance (number or percent),
        and answers with numeric answer_text and variables [{name, value}].
        Provide precomputed variants; formulas are not evaluated here. These
        fields follow Canvas's upstream parser, beyond its terse API schema.
        """
        payload = _question_payload(
            question_name=question_name,
            question_text=question_text,
            question_type=question_type,
            points_possible=points_possible,
            position=position,
            answers=answers,
            quiz_group_id=quiz_group_id,
            correct_comments=correct_comments,
            incorrect_comments=incorrect_comments,
            neutral_comments=neutral_comments,
            text_after_answers=text_after_answers,
            formulas=formulas,
            variables=variables,
            answer_tolerance=answer_tolerance,
        )
        if isinstance(payload, str):
            return payload
        course_id = await get_course_id(course_identifier)
        question = await make_canvas_request(
            "post",
            canvas_path('courses', course_id, 'quizzes', quiz_id, 'questions'),
            data={"question": payload},
        )
        if "error" in question:
            return f"Error creating quiz question: {question['error']}"
        return "Quiz question created:\n\n" + _format_question(question)

    @mcp.tool(annotations=ToolAnnotations(destructive_hint=True, idempotent_hint=True))
    @validate_params
    async def update_quiz_question(
        course_identifier: str | int,
        quiz_id: str | int,
        question_id: str | int,
        question_name: str | None = None,
        question_text: str | None = None,
        question_type: str | None = None,
        points_possible: float | None = None,
        position: int | None = None,
        answers: list[dict[str, Any]] | None = None,
        quiz_group_id: int | None = None,
        correct_comments: str | None = None,
        incorrect_comments: str | None = None,
        neutral_comments: str | None = None,
        text_after_answers: str | None = None,
        formulas: list[str] | None = None,
        variables: list[dict[str, Any]] | None = None,
        answer_tolerance: float | str | None = None,
    ) -> str:
        """Update a question definition in a Classic Quiz.

        For calculated fields, explicitly pass calculated_question plus the
        complete formulas/variables/tolerance/answers definition. See
        create_quiz_question for the supported upstream parser shape.
        """
        payload = _question_payload(
            question_name=question_name,
            question_text=question_text,
            question_type=question_type,
            points_possible=points_possible,
            position=position,
            answers=answers,
            quiz_group_id=quiz_group_id,
            correct_comments=correct_comments,
            incorrect_comments=incorrect_comments,
            neutral_comments=neutral_comments,
            text_after_answers=text_after_answers,
            formulas=formulas,
            variables=variables,
            answer_tolerance=answer_tolerance,
        )
        if isinstance(payload, str):
            return payload
        if not payload:
            return "No quiz-question fields were provided to update."
        course_id = await get_course_id(course_identifier)
        question = await make_canvas_request(
            "put",
            canvas_path('courses', course_id, 'quizzes', quiz_id, 'questions', question_id),
            data={"question": payload},
        )
        if "error" in question:
            return f"Error updating quiz question: {question['error']}"
        return "Quiz question updated:\n\n" + _format_question(question)

    @mcp.tool(annotations=ToolAnnotations(destructive_hint=True, idempotent_hint=True))
    @validate_params
    async def delete_quiz_question(
        course_identifier: str | int,
        quiz_id: str | int,
        question_id: str | int,
        allow_deleting_student_work: bool = False,
        confirmation_token: str | None = None,
    ) -> str:
        """Delete a quiz question after student-work checks and confirmation."""
        course_id = await get_course_id(course_identifier)
        quiz = await make_canvas_request(
            "get", canvas_path('courses', course_id, 'quizzes', quiz_id)
        )
        if "error" in quiz:
            return f"Error fetching quiz: {quiz['error']}"
        has_student_work = _has_student_quiz_work(quiz)
        if has_student_work and not allow_deleting_student_work:
            return _student_work_delete_error("quiz")
        question = await make_canvas_request(
            "get",
            canvas_path('courses', course_id, 'quizzes', quiz_id, 'questions', question_id),
        )
        if "error" in question:
            return f"Error fetching quiz question: {question['error']}"
        name = question.get("question_name") or "Unnamed question"
        shown_name = fence_untrusted_inline(name, "quiz question name")
        fingerprint = _DELETE_QUESTION_GUARD.fingerprint(
            "delete_quiz_question",
            str(course_id),
            str(quiz_id),
            str(question_id),
            name,
            str(question.get("question_text")),
            str(question.get("points_possible")),
            str(quiz.get("unpublishable")),
            str(allow_deleting_student_work),
        )
        if not confirmation_token:
            preview = (
                f"Would delete quiz question **{shown_name}** "
                f"(ID: {question_id}) from Classic Quiz {quiz_id}.\n"
                f"Student-work deletion authorized: "
                f"{'yes' if allow_deleting_student_work else 'not needed'}"
            )
            return preview_with_token(
                _DELETE_QUESTION_GUARD,
                fingerprint,
                "delete_quiz_question",
                preview,
            )
        error = redeem_confirmation(
            _DELETE_QUESTION_GUARD, confirmation_token, fingerprint
        )
        if error:
            return error
        response = await make_canvas_request(
            "delete",
            canvas_path('courses', course_id, 'quizzes', quiz_id, 'questions', question_id),
        )
        if isinstance(response, dict) and "error" in response:
            return f"Error deleting quiz question: {response['error']}"
        return f"Quiz question **{shown_name}** deleted."


    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    @validate_params
    async def list_quiz_question_groups(
        course_identifier: str | int, quiz_id: str | int
    ) -> str:
        """List Classic Quiz question groups, including linked bank IDs."""
        course_id = await get_course_id(course_identifier)
        response = await make_canvas_request(
            "get", canvas_path("courses", course_id, "quizzes", quiz_id, "groups")
        )
        return _format_groups_response(response)

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    @validate_params
    async def get_quiz_question_group(
        course_identifier: str | int, quiz_id: str | int, group_id: int
    ) -> str:
        """Read one Classic Quiz question group, without student attempts."""
        course_id = await get_course_id(course_identifier)
        response = await make_canvas_request(
            "get", canvas_path("courses", course_id, "quizzes", quiz_id, "groups", group_id)
        )
        if not isinstance(response, dict):
            return "Error: Canvas returned an invalid quiz group response."
        if "error" in response:
            return f"Error fetching quiz group: {response['error']}"
        return _format_group(response)

    @mcp.tool(annotations=ToolAnnotations(destructive_hint=False, idempotent_hint=False))
    @validate_params
    async def create_quiz_question_group(
        course_identifier: str | int, quiz_id: str | int, name: str,
        pick_count: int, question_points: float,
        assessment_question_bank_id: int | None = None,
    ) -> str:
        """Create a Classic question group, optionally linked to an accessible bank.

        Canvas validates bank access. This tool does not create or expose banks.
        Add questions with create_quiz_question using the resulting group ID.
        """
        payload = _group_payload(name, pick_count, question_points)
        if isinstance(payload, str):
            return payload
        if assessment_question_bank_id is not None:
            if assessment_question_bank_id <= 0:
                return "Error: assessment_question_bank_id must be positive."
            payload["assessment_question_bank_id"] = assessment_question_bank_id
        course_id = await get_course_id(course_identifier)
        response = await make_canvas_request(
            "post", canvas_path("courses", course_id, "quizzes", quiz_id, "groups"),
            data={"quiz_groups": [payload]},
        )
        return _format_groups_response(response)

    @mcp.tool(annotations=ToolAnnotations(destructive_hint=True, idempotent_hint=True))
    @validate_params
    async def update_quiz_question_group(
        course_identifier: str | int, quiz_id: str | int, group_id: int,
        name: str | None = None, pick_count: int | None = None,
        question_points: float | None = None,
    ) -> str:
        """Update a group's name, question count, or points per question.

        Canvas does not document changing a group's bank association.
        """
        payload = _group_payload(name, pick_count, question_points)
        if isinstance(payload, str):
            return payload
        if not payload:
            return "Error: no quiz-group changes specified."
        course_id = await get_course_id(course_identifier)
        response = await make_canvas_request(
            "put", canvas_path("courses", course_id, "quizzes", quiz_id, "groups", group_id),
            data={"quiz_groups": [payload]},
        )
        return _format_groups_response(response)

    @mcp.tool(annotations=ToolAnnotations(destructive_hint=True, idempotent_hint=True))
    @validate_params
    async def delete_quiz_question_group(
        course_identifier: str | int, quiz_id: str | int, group_id: int,
        allow_deleting_student_work: bool = False,
        confirmation_token: str | None = None,
    ) -> str:
        """Preview and confirm deletion of a group and its contained questions.

        Existing student work requires explicit opt-in; no attempts are read.
        """
        course_id = await get_course_id(course_identifier)
        quiz_path = canvas_path("courses", course_id, "quizzes", quiz_id)
        quiz = await make_canvas_request("get", quiz_path)
        if not isinstance(quiz, dict) or any(type(quiz.get(k)) is not bool for k in ("published", "unpublishable")):
            return "Error: Canvas did not establish the quiz student-work state. Nothing was deleted."
        if "error" in quiz:
            return f"Error fetching quiz: {quiz['error']}"
        if _has_student_quiz_work(quiz) and not allow_deleting_student_work:
            return _student_work_delete_error("quiz")
        group_path = canvas_path("courses", course_id, "quizzes", quiz_id, "groups", group_id)
        group = await make_canvas_request("get", group_path)
        if not isinstance(group, dict) or group.get("id") != group_id:
            return "Error: Canvas did not return the requested quiz group. Nothing was deleted."
        if "error" in group:
            return f"Error fetching quiz group: {group['error']}"
        questions = await fetch_all_paginated_results(
            canvas_path("courses", course_id, "quizzes", quiz_id, "questions")
        )
        if isinstance(questions, dict) and "error" in questions:
            return f"Error fetching group questions: {questions['error']}"
        if not isinstance(questions, list) or any(not isinstance(q, dict) or type(q.get("id")) is not int for q in questions):
            return "Error: Canvas returned an invalid question list. Nothing was deleted."
        members = sorted(
            (q for q in questions if str(q.get("quiz_group_id")) == str(group_id)),
            key=lambda q: str(q.get("id")),
        )
        fingerprint = _DELETE_GROUP_GUARD.fingerprint(
            "delete_quiz_question_group", str(course_id), str(quiz_id), str(group_id),
            json.dumps(group, sort_keys=True, default=str),
            json.dumps(members, sort_keys=True, default=str),
            str(quiz.get("published")), str(quiz.get("unpublishable")),
            str(allow_deleting_student_work),
        )
        if not confirmation_token:
            preview = (f"Would delete quiz group and {len(members)} contained question(s).\n"
                       + _format_group(group)
                       + f"\nStudent-work deletion authorized: {allow_deleting_student_work}")
            return preview_with_token(
                _DELETE_GROUP_GUARD, fingerprint, "delete_quiz_question_group", preview
            )
        error = redeem_confirmation(_DELETE_GROUP_GUARD, confirmation_token, fingerprint)
        if error:
            return error
        response = await make_canvas_request("delete", group_path)
        if isinstance(response, dict) and "error" in response:
            return f"Error deleting quiz group: {response['error']}"
        return f"Deleted quiz question group {group_id}."

    @mcp.tool(annotations=ToolAnnotations(destructive_hint=True, idempotent_hint=True))
    @validate_params
    async def reorder_quiz_items(
        course_identifier: str | int, quiz_id: str | int,
        order: list[dict[str, Any]], group_id: int | None = None,
    ) -> str:
        """Reorder Classic questions/groups, or questions inside one group.

        Each order entry requires positive integer id and type question/group.
        For group_id, only type question is allowed. Canvas validates ownership.
        This tool rejects unknown items and questions outside the selected
        group, preventing Canvas reorder from silently changing membership.
        Use update_quiz_question to change a question's group.
        """
        if not order:
            return "Error: order must contain at least one item."
        seen = set()
        for item in order:
            if (set(item) != {"id", "type"}
                or type(item["id"]) is not int or item["id"] <= 0
                or item["type"] not in ("question", "group")
                or (group_id is not None and item["type"] != "question")):
                return "Error: order entries require a positive integer id and valid type."
            identity = (item["type"], item["id"])
            if identity in seen:
                return "Error: duplicate item in order."
            seen.add(identity)
        if group_id is not None and group_id <= 0:
            return "Error: group_id must be positive."
        course_id = await get_course_id(course_identifier)
        questions = await fetch_all_paginated_results(
            canvas_path("courses", course_id, "quizzes", quiz_id, "questions")
        )
        if isinstance(questions, dict) and "error" in questions:
            return f"Error fetching quiz questions: {questions['error']}"
        if not isinstance(questions, list) or any(not isinstance(q, dict) or type(q.get("id")) is not int for q in questions):
            return "Error: Canvas returned an invalid question list. Nothing was reordered."
        groups_response = await make_canvas_request(
            "get", canvas_path("courses", course_id, "quizzes", quiz_id, "groups")
        )
        if not isinstance(groups_response, dict):
            return "Error: Canvas returned an invalid quiz_groups response."
        if "error" in groups_response:
            return f"Error fetching quiz groups: {groups_response['error']}"
        groups = groups_response.get("quiz_groups")
        if not isinstance(groups, list) or any(not isinstance(g, dict) or type(g.get("id")) is not int for g in groups):
            return "Error: Canvas returned an invalid quiz_groups response."
        group_ids = {g.get("id") for g in groups}
        if group_id is not None and group_id not in group_ids:
            return "Error: group_id does not belong to this quiz."
        question_map = {q.get("id"): q for q in questions}
        for item in order:
            if item["type"] == "group":
                if item["id"] not in group_ids:
                    return "Error: an ordered group does not belong to this quiz."
            else:
                question = question_map.get(item["id"])
                if question is None:
                    return "Error: an ordered question does not belong to this quiz."
                if question.get("quiz_group_id") != group_id:
                    return "Error: reorder would change question group membership. Use update_quiz_question explicitly."
        segments = ["courses", course_id, "quizzes", quiz_id]
        if group_id is not None:
            segments.extend(["groups", group_id])
        response = await make_canvas_request(
            "post", canvas_path(*segments, "reorder"), data={"order": order}
        )
        if isinstance(response, dict) and "error" in response:
            return f"Error reordering quiz items: {response['error']}"
        return "Classic Quiz items reordered."
