"""Classic Quiz authoring tools that do not read student activity."""

import json
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
) -> dict[str, Any] | str:
    if any(
        contains_fence_markers(value)
        for value in (
            question_name, question_text, answers, correct_comments,
            incorrect_comments, neutral_comments, text_after_answers,
        )
        if value is not None
    ):
        return FENCE_LEAK_ERROR
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
    }
    return {key: value for key, value in values.items() if value is not None}


def register_quiz_tools(mcp: FastMCP) -> None:
    """Register Classic Quiz definition and question authoring tools."""

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    @validate_params
    async def list_quizzes(course_identifier: str | int) -> str:
        """List Classic Quizzes without submissions, attempts, or statistics."""
        course_id = await get_course_id(course_identifier)
        quizzes = await fetch_all_paginated_results(
            canvas_path('courses', course_id, 'quizzes'), {"per_page": 100}
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
    ) -> str:
        """Add a question definition to a Classic Quiz."""
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
    ) -> str:
        """Update a question definition in a Classic Quiz."""
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
