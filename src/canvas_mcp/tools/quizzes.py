"""Classic Quiz authoring tools that do not read student activity."""

import json
from typing import Any

from fastmcp import FastMCP
from mcp.types import ToolAnnotations

from ..core.cache import get_course_code, get_course_id
from ..core.client import fetch_all_paginated_results, make_canvas_request
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
        f"Due: {quiz.get('due_at') or 'No due date'}",
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
            "Question text:\n"
            + fence_untrusted(
                question.get("question_text") or "", "quiz question text"
            ),
    ]
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
    allowed_attempts: int | None = None,
    scoring_policy: str | None = None,
    one_question_at_a_time: bool | None = None,
    cant_go_back: bool | None = None,
    due_at: str | None = None,
    unlock_at: str | None = None,
    lock_at: str | None = None,
    published: bool | None = None,
) -> dict[str, Any] | str:
    if (title is not None and contains_fence_markers(title)) or (
        description is not None and contains_fence_markers(description)
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

    values = {
        "title": title,
        "description": description,
        "quiz_type": quiz_type,
        "assignment_group_id": assignment_group_id,
        "time_limit": time_limit,
        "shuffle_answers": shuffle_answers,
        "allowed_attempts": allowed_attempts,
        "scoring_policy": scoring_policy,
        "one_question_at_a_time": one_question_at_a_time,
        "cant_go_back": cant_go_back,
        "due_at": due_at,
        "unlock_at": unlock_at,
        "lock_at": lock_at,
        "published": published,
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
) -> dict[str, Any] | str:
    if any(
        contains_fence_markers(value)
        for value in (question_name, question_text, answers)
        if value is not None
    ):
        return FENCE_LEAK_ERROR
    values = {
        "question_name": question_name,
        "question_text": question_text,
        "question_type": question_type,
        "points_possible": points_possible,
        "position": position,
        "answers": answers,
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
            f"/courses/{course_id}/quizzes", {"per_page": 100}
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
            "get", f"/courses/{course_id}/quizzes/{quiz_id}"
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
        allowed_attempts: int | None = None,
        scoring_policy: str | None = None,
        one_question_at_a_time: bool | None = None,
        cant_go_back: bool | None = None,
        due_at: str | None = None,
        unlock_at: str | None = None,
        lock_at: str | None = None,
        published: bool = False,
    ) -> str:
        """Create a Classic Quiz; new quizzes default to unpublished."""
        payload = _quiz_payload(
            title=title,
            description=description,
            quiz_type=quiz_type,
            assignment_group_id=assignment_group_id,
            time_limit=time_limit,
            shuffle_answers=shuffle_answers,
            allowed_attempts=allowed_attempts,
            scoring_policy=scoring_policy,
            one_question_at_a_time=one_question_at_a_time,
            cant_go_back=cant_go_back,
            due_at=due_at,
            unlock_at=unlock_at,
            lock_at=lock_at,
            published=published,
        )
        if isinstance(payload, str):
            return payload
        course_id = await get_course_id(course_identifier)
        quiz = await make_canvas_request(
            "post", f"/courses/{course_id}/quizzes", data={"quiz": payload}
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
        allowed_attempts: int | None = None,
        scoring_policy: str | None = None,
        one_question_at_a_time: bool | None = None,
        cant_go_back: bool | None = None,
        due_at: str | None = None,
        unlock_at: str | None = None,
        lock_at: str | None = None,
        published: bool | None = None,
        clear_time_limit: bool = False,
        clear_due_at: bool = False,
        clear_unlock_at: bool = False,
        clear_lock_at: bool = False,
    ) -> str:
        """Update a Classic Quiz definition, including clearing its dates."""
        if time_limit is not None and clear_time_limit:
            return (
                "Invalid configuration: time_limit and clear_time_limit cannot "
                "both be provided."
            )

        date_updates = (
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

        payload = _quiz_payload(
            title=title,
            description=description,
            quiz_type=quiz_type,
            assignment_group_id=assignment_group_id,
            time_limit=time_limit,
            shuffle_answers=shuffle_answers,
            allowed_attempts=allowed_attempts,
            scoring_policy=scoring_policy,
            one_question_at_a_time=one_question_at_a_time,
            cant_go_back=cant_go_back,
            due_at=due_at,
            unlock_at=unlock_at,
            lock_at=lock_at,
            published=published,
        )
        if isinstance(payload, str):
            return payload
        if clear_time_limit:
            payload["time_limit"] = None
        for field_name, _value, clear in date_updates:
            if clear:
                payload[field_name] = None
        if not payload:
            return "No quiz fields were provided to update."
        course_id = await get_course_id(course_identifier)
        quiz = await make_canvas_request(
            "put",
            f"/courses/{course_id}/quizzes/{quiz_id}",
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
            "get", f"/courses/{course_id}/quizzes/{quiz_id}"
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
            "delete", f"/courses/{course_id}/quizzes/{quiz_id}"
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
            f"/courses/{course_id}/quizzes/{quiz_id}/questions",
            {"per_page": 100},
        )
        if isinstance(questions, dict) and "error" in questions:
            return f"Error listing quiz questions: {questions['error']}"
        if not questions:
            return f"No questions found for Classic Quiz {quiz_id}."
        return f"Questions for Classic Quiz {quiz_id}:\n\n" + "\n\n".join(
            _format_question(question) for question in questions
        )

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
    ) -> str:
        """Add a question definition to a Classic Quiz."""
        payload = _question_payload(
            question_name=question_name,
            question_text=question_text,
            question_type=question_type,
            points_possible=points_possible,
            position=position,
            answers=answers,
        )
        if isinstance(payload, str):
            return payload
        course_id = await get_course_id(course_identifier)
        question = await make_canvas_request(
            "post",
            f"/courses/{course_id}/quizzes/{quiz_id}/questions",
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
    ) -> str:
        """Update a question definition in a Classic Quiz."""
        payload = _question_payload(
            question_name=question_name,
            question_text=question_text,
            question_type=question_type,
            points_possible=points_possible,
            position=position,
            answers=answers,
        )
        if isinstance(payload, str):
            return payload
        if not payload:
            return "No quiz-question fields were provided to update."
        course_id = await get_course_id(course_identifier)
        question = await make_canvas_request(
            "put",
            f"/courses/{course_id}/quizzes/{quiz_id}/questions/{question_id}",
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
            "get", f"/courses/{course_id}/quizzes/{quiz_id}"
        )
        if "error" in quiz:
            return f"Error fetching quiz: {quiz['error']}"
        has_student_work = _has_student_quiz_work(quiz)
        if has_student_work and not allow_deleting_student_work:
            return _student_work_delete_error("quiz")
        question = await make_canvas_request(
            "get",
            f"/courses/{course_id}/quizzes/{quiz_id}/questions/{question_id}",
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
            f"/courses/{course_id}/quizzes/{quiz_id}/questions/{question_id}",
        )
        if isinstance(response, dict) and "error" in response:
            return f"Error deleting quiz question: {response['error']}"
        return f"Quiz question **{shown_name}** deleted."
