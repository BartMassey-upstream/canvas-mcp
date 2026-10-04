"""Assignment-related MCP tools for Canvas API."""

import asyncio
import datetime
import json
import math
import re
from statistics import StatisticsError, mean, median, stdev
from typing import Any, TypeGuard
from urllib.parse import urlsplit

from fastmcp import FastMCP
from mcp.types import ToolAnnotations

from ..core.cache import get_course_code, get_course_id
from ..core.client import fetch_all_paginated_results, make_canvas_request
from ..core.config import get_config
from ..core.dates import format_date, parse_date
from ..core.path import canvas_path
from ..core.untrusted_content import (
    FENCE_LEAK_ERROR,
    contains_fence_markers,
    fence_untrusted,
    fence_untrusted_inline,
)
from ..core.validation import coerce_canvas_id, validate_params
from ..core.write_confirmation import (
    ConfirmationGuard,
    preview_with_token,
    redeem_confirmation,
)
from ..core.write_outcome import RequestFailure, WriteOutcome
from .rubrics import (
    build_rubric_assessment_form_data,
    rubric_grade_is_confirmed,
)

_DELETE_ASSIGNMENT_GUARD = ConfirmationGuard(nothing_done="Nothing was deleted.")


def _finite_grade_number(value: object) -> TypeGuard[int | float]:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _bulk_grading_preflight(
    grades: dict[str, Any], max_concurrent: int, rate_limit_delay: float
) -> str | None:
    if (
        not isinstance(max_concurrent, int)
        or isinstance(max_concurrent, bool)
        or not 1 <= max_concurrent <= 20
    ):
        return "Error: max_concurrent must be an integer between 1 and 20. No grades were submitted."
    if not _finite_grade_number(rate_limit_delay) or rate_limit_delay < 0:
        return "Error: rate_limit_delay must be a finite nonnegative number. No grades were submitted."
    if not isinstance(grades, dict) or not grades:
        return "Error: No grades provided. The grades dictionary is empty."
    seen_targets = set()
    for user_id, info in grades.items():
        if coerce_canvas_id(user_id) != str(user_id) or int(str(user_id)) <= 0:
            return "Error: Every grading target must be a positive Canvas user ID. No grades were submitted."
        canonical_target = str(int(user_id))
        if canonical_target in seen_targets:
            return "Error: Duplicate Canvas user IDs are not allowed in one grading batch. No grades were submitted."
        seen_targets.add(canonical_target)
        if not isinstance(info, dict) or set(info) - {
            "grade",
            "rubric_assessment",
            "comment",
            "expected_attempt",
        }:
            return "Error: Each grade must be an object containing only grade, rubric_assessment, comment, and expected_attempt. No grades were submitted."
        if "expected_attempt" in info:
            expected = info["expected_attempt"]
            if expected is not None and (
                isinstance(expected, bool)
                or not isinstance(expected, int)
                or expected < 0
                or not _finite_grade_number(expected)
            ):
                return "Error: expected_attempt must be a finite nonnegative integer or null. No grades were submitted."
        comment = info.get("comment")
        if comment is not None and not isinstance(comment, str):
            return "Error: Submission comments must be strings or null. No grades were submitted."
        if contains_fence_markers(comment or ""):
            return FENCE_LEAK_ERROR + " No grades were submitted."
        if "grade" in info:
            grade = info["grade"]
            if (
                isinstance(grade, bool)
                or not isinstance(grade, str | int | float)
                or isinstance(grade, str)
                and not grade.strip()
            ):
                return "Error: Grades must be nonempty strings or finite numbers. No grades were submitted."
            if contains_fence_markers(str(grade)):
                return FENCE_LEAK_ERROR + " No grades were submitted."
            try:
                number = float(str(grade).strip().removesuffix("%"))
            except ValueError:
                number = None
            except OverflowError:
                return "Error: Numeric grades must be finite. No grades were submitted."
            if number is not None and not math.isfinite(number):
                return "Error: Numeric grades must be finite. No grades were submitted."
        rubric = info.get("rubric_assessment")
        if "rubric_assessment" in info:
            if not isinstance(rubric, dict) or not rubric:
                return "Error: rubric_assessment must be a nonempty criterion object. No grades were submitted."
            total = 0.0
            for criterion_id, assessment in rubric.items():
                if (
                    not isinstance(criterion_id, str)
                    or re.fullmatch(r"[A-Za-z0-9_-]+", criterion_id) is None
                ):
                    return "Error: Rubric criterion IDs must be nonempty safe identifiers. No grades were submitted."
                if not isinstance(assessment, dict) or set(assessment) - {
                    "points",
                    "rating_id",
                    "comments",
                }:
                    return "Error: Each rubric criterion must contain only points, rating_id, and comments. No grades were submitted."
                points = assessment.get("points")
                if not _finite_grade_number(points) or points < 0:
                    return "Error: Rubric points must be finite nonnegative numbers. No grades were submitted."
                total += points
                rating_id = assessment.get("rating_id")
                if "rating_id" in assessment and (
                    isinstance(rating_id, bool)
                    or not isinstance(rating_id, str | int)
                    or re.fullmatch(r"[A-Za-z0-9_-]+", str(rating_id)) is None
                ):
                    return "Error: Rubric rating IDs must be nonempty safe identifiers. No grades were submitted."
                criterion_comment = assessment.get("comments", "")
                if not isinstance(criterion_comment, str):
                    return "Error: Rubric comments must be strings. No grades were submitted."
                if contains_fence_markers(criterion_comment):
                    return FENCE_LEAK_ERROR + " No grades were submitted."
            if not math.isfinite(total):
                return "Error: Total rubric points must be finite. No grades were submitted."
        elif "grade" not in info:
            return "Error: Each target requires rubric_assessment or grade. No grades were submitted."
    return None


def _grading_id(value: object) -> str | None:
    if isinstance(value, bool) or not isinstance(value, str | int):
        return None
    result = coerce_canvas_id(value)
    return result if result is not None and int(result) > 0 else None


def _analytics_timestamp(value: object) -> datetime.datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=datetime.UTC) if parsed.tzinfo is None else parsed
    except ValueError:
        return None


def _grading_submission_identity(
    value: object, assignment_id: str, user_id: str
) -> dict[str, Any] | None:
    if not isinstance(value, dict) or "error" in value:
        return None
    if (
        _grading_id(value.get("assignment_id")) != assignment_id
        or _grading_id(value.get("user_id")) != user_id
    ):
        return None
    return value


def _grading_attempt_available(
    submission: dict[str, Any], *, readback: bool = False
) -> bool:
    if "attempt" not in submission:
        return False
    attempt = submission["attempt"]
    if attempt is None:
        states = {"unsubmitted", "graded"} if readback else {"unsubmitted"}
        return (
            submission.get("workflow_state") in states
            and "submitted_at" in submission
            and submission["submitted_at"] is None
        )
    return (
        isinstance(attempt, int)
        and not isinstance(attempt, bool)
        and attempt >= 0
        and _finite_grade_number(attempt)
    )


def _grading_policy_failure(response: object) -> bool:
    return isinstance(response, RequestFailure) and response.status_code in {401, 403}


def _persisted_grade_matches(
    assignment: dict[str, Any], info: dict[str, Any], submission: dict[str, Any]
) -> bool:
    if submission.get("grade_matches_current_submission") is not True:
        return False
    rubric = info.get("rubric_assessment")
    if rubric:
        persisted = submission.get("rubric_assessment")
        if not isinstance(persisted, dict):
            return False
        for criterion_id, criterion in rubric.items():
            actual = persisted.get(criterion_id)
            if not isinstance(actual, dict):
                return False
            for field, value in criterion.items():
                observed = actual.get(field)
                if field == "points":
                    if not _finite_grade_number(observed) or not math.isclose(
                        observed, value, rel_tol=1e-9, abs_tol=1e-6
                    ):
                        return False
                elif field == "rating_id":
                    if str(observed) != str(value):
                        return False
                elif observed != value:
                    return False
        return rubric_grade_is_confirmed(assignment, rubric, submission)
    grade = str(info["grade"]).strip()
    expected: float | None = None
    try:
        expected = float(grade)
    except ValueError:
        points = assignment.get("points_possible")
        if grade.endswith("%") and _finite_grade_number(points):
            try:
                expected = float(grade[:-1]) * points / 100
            except ValueError:
                return False
        elif grade.lower() in {
            "pass",
            "complete",
            "fail",
            "incomplete",
        } and _finite_grade_number(points):
            expected = points if grade.lower() in {"pass", "complete"} else 0
        else:
            return (
                isinstance(submission.get("grade"), str)
                and submission["grade"].strip() == grade
            )
    score = submission.get("score")
    return (
        expected is not None
        and math.isfinite(expected)
        and _finite_grade_number(score)
        and math.isclose(score, expected, rel_tol=1e-9, abs_tol=1e-6)
        and submission.get("grade") is not None
    )


def _grading_comment_ids(value: object) -> set[str] | None:
    if not isinstance(value, list):
        return None
    ids = set()
    for item in value:
        if not isinstance(item, dict) or _grading_id(item.get("id")) is None:
            return None
        item_id = str(item["id"])
        if item_id in ids:
            return None
        ids.add(item_id)
    return ids


def _assignment_authoring_options(
    allowed_attempts: int | None,
    position: int | None,
    external_tool_url: str | None,
    external_tool_new_tab: bool | None,
    omit_from_final_grade: bool | None,
    hide_in_gradebook: bool | None,
    grading_standard_id: int | None,
    annotatable_attachment_id: int | None,
) -> dict[str, Any] | str:
    if allowed_attempts is not None and allowed_attempts != -1 and allowed_attempts < 1:
        return "Error: allowed_attempts must be positive or -1 for unlimited."
    for key, value in (
        ("position", position),
        ("grading_standard_id", grading_standard_id),
        ("annotatable_attachment_id", annotatable_attachment_id),
    ):
        if value is not None and value < 1:
            return f"Error: {key} must be positive."
    data: dict[str, Any] = {
        key: value
        for key, value in (
            ("allowed_attempts", allowed_attempts),
            ("position", position),
            ("omit_from_final_grade", omit_from_final_grade),
            ("hide_in_gradebook", hide_in_gradebook),
            ("grading_standard_id", grading_standard_id),
            ("annotatable_attachment_id", annotatable_attachment_id),
        )
        if value is not None
    }
    external: dict[str, Any] = {}
    if external_tool_url is not None:
        if contains_fence_markers(external_tool_url):
            return FENCE_LEAK_ERROR
        try:
            url = urlsplit(external_tool_url)
            if (
                url.scheme not in {"http", "https"}
                or not url.hostname
                or url.username
                or url.password
            ):
                return "Error: external_tool_url must be an HTTP(S) URL without credentials."
        except ValueError:
            return "Error: external_tool_url is invalid."
        external["url"] = external_tool_url
    if external_tool_new_tab is not None:
        external["new_tab"] = external_tool_new_tab
    if external:
        data["external_tool_tag_attributes"] = external
    return data


def register_shared_assignment_tools(mcp: FastMCP) -> None:
    """Register assignment tools accessible to both students and educators."""

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    @validate_params
    async def list_assignments(course_identifier: str | int) -> str:
        """List assignments for a specific course.

        Args:
            course_identifier: Course code or Canvas ID
        """
        course_id = await get_course_id(course_identifier)

        params: dict[str, Any] = {"per_page": 100}
        if get_config().canvas_role != "creator":
            params["include[]"] = ["all_dates", "submission"]

        all_assignments = await fetch_all_paginated_results(
            canvas_path("courses", course_id, "assignments"), params
        )

        if isinstance(all_assignments, dict) and "error" in all_assignments:
            return f"Error fetching assignments: {all_assignments['error']}"

        if not all_assignments:
            return f"No assignments found for course {course_identifier}."

        assignments_info = []
        for assignment in all_assignments:
            assignment_id = assignment.get("id")
            name = assignment.get("name", "Unnamed assignment")
            due_at = assignment.get("due_at", "No due date")
            points = assignment.get("points_possible", 0)
            assignment_group_id = assignment.get("assignment_group_id", "N/A")

            # Assignment names are instructor-authored free text (issue 239).
            assignments_info.append(
                f"ID: {assignment_id}\n"
                f"Name: {fence_untrusted_inline(name, 'assignment name')}\n"
                f"Assignment Group ID: {assignment_group_id}\n"
                f"Due: {due_at}\nPoints: {points}\n"
            )

        # Try to get the course code for display
        course_display = await get_course_code(course_id) or course_identifier
        return f"Assignments for Course {course_display}:\n\n" + "\n".join(
            assignments_info
        )

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    @validate_params
    async def get_assignment_details(
        course_identifier: str | int, assignment_id: str | int
    ) -> str:
        """Get detailed information about a specific assignment.

        Args:
            course_identifier: Course code or Canvas ID
            assignment_id: Canvas assignment ID
        """
        course_id = await get_course_id(course_identifier)

        # Ensure assignment_id is a string
        assignment_id_str = str(assignment_id)

        response = await make_canvas_request(
            "get", canvas_path("courses", course_id, "assignments", assignment_id_str)
        )

        if "error" in response:
            return f"Error fetching assignment details: {response['error']}"

        # Name and description are author-controlled; the description is full
        # HTML that can carry paragraphs of injected directives (issue 239).
        details = [
            f"Name: {fence_untrusted_inline(response.get('name', 'N/A'), 'assignment name')}",
            "Description:\n"
            + fence_untrusted(
                response.get("description") or "N/A", "assignment description"
            ),
            f"Due Date: {format_date(response.get('due_at'))}",
            f"Points Possible: {response.get('points_possible', 'N/A')}",
            f"Assignment Group ID: {response.get('assignment_group_id', 'N/A')}",
            f"Submission Types: {', '.join(response.get('submission_types', ['N/A']))}",
            f"Published: {response.get('published', False)}",
            f"Locked: {response.get('locked_for_user', False)}",
        ]

        # Try to get the course code for display
        course_display = await get_course_code(course_id) or course_identifier
        return (
            f"Assignment Details for ID {assignment_id} in course {course_display}:\n\n"
            + "\n".join(details)
        )


def register_educator_assignment_tools(mcp: FastMCP) -> None:
    """Register educator-only assignment tools (grading, analytics, management)."""

    @mcp.tool(
        annotations=ToolAnnotations(destructive_hint=False, idempotent_hint=False)
    )
    @validate_params
    async def assign_peer_review(
        course_identifier: str, assignment_id: str, reviewer_id: str, reviewee_id: str
    ) -> str:
        """Manually assign a peer review to a student for a specific assignment.

        Args:
            course_identifier: Course code or Canvas ID
            assignment_id: Canvas assignment ID
            reviewer_id: User ID of the student who will review
            reviewee_id: User ID of the student whose submission will be reviewed
        """
        course_id = await get_course_id(course_identifier)

        # First, we need to get the submission ID for the reviewee
        submissions = await make_canvas_request(
            "get",
            canvas_path(
                "courses", course_id, "assignments", assignment_id, "submissions"
            ),
            params={"per_page": 100},
        )

        if "error" in submissions:
            return f"Error fetching submissions: {submissions['error']}"

        # Find the submission for the reviewee
        reviewee_submission = None
        for submission in submissions:
            if str(submission.get("user_id")) == str(reviewee_id):
                reviewee_submission = submission
                break

        # If no submission exists, we need to create a placeholder submission
        if not reviewee_submission:
            # Create a placeholder submission for the reviewee
            placeholder_data = {
                "submission": {
                    "user_id": reviewee_id,
                    "submission_type": "online_text_entry",
                    "body": "Placeholder submission for peer review",
                }
            }

            reviewee_submission = await make_canvas_request(
                "post",
                canvas_path(
                    "courses", course_id, "assignments", assignment_id, "submissions"
                ),
                data=placeholder_data,
            )

            if "error" in reviewee_submission:
                return f"Error creating placeholder submission: {reviewee_submission['error']}"

        # Now assign the peer review using the submission ID
        submission_id = reviewee_submission.get("id")

        # Data for the peer review assignment
        data = {
            "user_id": reviewer_id  # The user who will do the review
        }

        # Make the API request to create the peer review
        response = await make_canvas_request(
            "post",
            canvas_path(
                "courses",
                course_id,
                "assignments",
                assignment_id,
                "submissions",
                submission_id,
                "peer_reviews",
            ),
            data=data,
        )

        if "error" in response:
            return f"Error assigning peer review: {response['error']}"

        # Try to get the course code for display
        course_display = await get_course_code(course_id) or course_identifier

        return (
            f"Successfully assigned peer review in course {course_display}:\n"
            + f"Assignment ID: {assignment_id}\n"
            + f"Reviewer ID: {reviewer_id}\n"
            + f"Reviewee ID: {reviewee_id}\n"
            + f"Submission ID: {submission_id}"
        )

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    @validate_params
    async def list_peer_reviews(course_identifier: str, assignment_id: str) -> str:
        """List all peer review assignments for a specific assignment.

        Args:
            course_identifier: Course code or Canvas ID
            assignment_id: Canvas assignment ID
        """
        course_id = await get_course_id(course_identifier)

        # Get all submissions for this assignment
        submissions = await fetch_all_paginated_results(
            canvas_path(
                "courses", course_id, "assignments", assignment_id, "submissions"
            ),
            {"include[]": "submission_comments", "per_page": 100},
        )

        if isinstance(submissions, dict) and "error" in submissions:
            return f"Error fetching submissions: {submissions['error']}"

        if not submissions:
            return f"No submissions found for assignment {assignment_id}."

        # Anonymization happens at the client layer (core/client.py) per
        # ENABLE_DATA_ANONYMIZATION (#179)

        # Get all users in the course for name lookups
        users = await fetch_all_paginated_results(
            canvas_path("courses", course_id, "users"), {"per_page": 100}
        )

        if isinstance(users, dict) and "error" in users:
            return f"Error fetching users: {users['error']}"

        # Create a mapping of user IDs to names
        user_map = {}
        for user in users:
            user_id = str(user.get("id"))
            user_name = user.get("name", "Unknown")
            user_map[user_id] = user_name

        # Collect peer review data
        peer_reviews_by_submission = {}

        for submission in submissions:
            submission_id = submission.get("id")
            user_id = str(submission.get("user_id"))
            user_name = user_map.get(user_id, f"User {user_id}")

            # Get peer reviews for this submission
            peer_reviews = await make_canvas_request(
                "get",
                canvas_path(
                    "courses",
                    course_id,
                    "assignments",
                    assignment_id,
                    "submissions",
                    submission_id,
                    "peer_reviews",
                ),
            )

            if "error" in peer_reviews:
                continue  # Skip if error

            if peer_reviews:
                peer_reviews_by_submission[submission_id] = {
                    "user_id": user_id,
                    "user_name": user_name,
                    "peer_reviews": peer_reviews,
                }

        # Format the output
        course_display = await get_course_code(course_id) or course_identifier
        output = f"Peer Reviews for Assignment {assignment_id} in course {course_display}:\n\n"

        if not peer_reviews_by_submission:
            output += "No peer reviews found for this assignment."
            return output

        # Display peer reviews grouped by reviewee
        for _submission_id, data in peer_reviews_by_submission.items():
            reviewee_name = data["user_name"]
            reviewee_id = data["user_id"]
            reviews = data["peer_reviews"]

            output += (
                f"Reviews for {fence_untrusted_inline(reviewee_name, 'student name')} "
                f"(ID: {reviewee_id}):\n"
            )

            if not reviews:
                output += "  No peer reviews assigned.\n\n"
                continue

            for review in reviews:
                reviewer_id = str(review.get("user_id"))
                reviewer_name = user_map.get(reviewer_id, f"User {reviewer_id}")
                workflow_state = review.get("workflow_state", "Unknown")

                output += f"  Reviewer: {fence_untrusted_inline(reviewer_name, 'student name')} (ID: {reviewer_id})\n"
                output += f"  Status: {workflow_state}\n"

                # Add assessment details if available
                if "assessment" in review and review["assessment"]:
                    assessment = review["assessment"]
                    score = assessment.get("score")
                    if score is not None:
                        output += f"  Score: {score}\n"

                output += "\n"

        return output

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    @validate_params
    async def list_submissions(
        course_identifier: str | int, assignment_id: str | int
    ) -> str:
        """List every submission record for one assignment.

        Returns one record per student (user ID, submitted-at time, score,
        grade), including students who have not submitted. Does not return
        submission content, attachments, or comments; use
        get_rubric_assessment for a student's rubric scores.

        Args:
            course_identifier: Course code or Canvas ID
            assignment_id: Canvas assignment ID
        """
        course_id = await get_course_id(course_identifier)

        # Ensure assignment_id is a string
        assignment_id_str = str(assignment_id)

        params = {"per_page": 100}

        submissions = await fetch_all_paginated_results(
            canvas_path(
                "courses", course_id, "assignments", assignment_id_str, "submissions"
            ),
            params,
        )

        if isinstance(submissions, dict) and "error" in submissions:
            return f"Error fetching submissions: {submissions['error']}"

        if not submissions:
            return f"No submissions found for assignment {assignment_id}."

        # Anonymization happens at the client layer (core/client.py) per
        # ENABLE_DATA_ANONYMIZATION (#179)

        submissions_info = []
        for submission in submissions:
            user_id = submission.get("user_id")
            submitted_at = submission.get("submitted_at", "Not submitted")
            score = submission.get("score", "Not graded")
            grade = submission.get("grade", "Not graded")

            submissions_info.append(
                f"User ID: {user_id}\nSubmitted: {submitted_at}\nScore: {score}\nGrade: {grade}\n"
            )

        # Try to get the course code for display
        course_display = await get_course_code(course_id) or course_identifier
        return (
            f"Submissions for Assignment {assignment_id} in course {course_display}:\n\n"
            + "\n".join(submissions_info)
        )

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    @validate_params
    async def get_assignment_analytics(
        course_identifier: str | int, assignment_id: str | int
    ) -> str:
        """Get detailed analytics about student performance on a specific assignment.

        Args:
            course_identifier: Course code or Canvas ID
            assignment_id: Canvas assignment ID
        """
        observed_start = datetime.datetime.now(datetime.UTC).isoformat()
        course_id = await get_course_id(course_identifier)
        canvas_origin = urlsplit(get_config().canvas_api_url)
        hostname = canvas_origin.hostname or ""
        host = f"[{hostname}]" if ":" in hostname else hostname
        port = f":{canvas_origin.port}" if canvas_origin.port is not None else ""
        origin = f"{canvas_origin.scheme}://{host}{port}"
        assignment_url = origin + canvas_path(
            "courses", course_id, "assignments", assignment_id
        )
        api_base = origin + canvas_origin.path.rstrip("/")
        source_urls = [
            assignment_url,
            api_base + canvas_path("courses", course_id, "assignments", assignment_id),
            api_base + canvas_path("courses", course_id, "users"),
            api_base
            + canvas_path(
                "courses", course_id, "assignments", assignment_id, "submissions"
            ),
        ]

        def observation_context() -> str:
            return (
                f"Observation started: {observed_start}\n"
                f"Observation completed: {datetime.datetime.now(datetime.UTC).isoformat()}\n"
                "Sources:\n"
                + "\n".join(f"  {url}" for url in source_urls)
                + "\nScope: visible student roster and returned assignment submissions; reads are not atomic.\n"
            )

        # Ensure assignment_id is a string
        assignment_id_str = str(assignment_id)

        # Get assignment details
        assignment = await make_canvas_request(
            "get", canvas_path("courses", course_id, "assignments", assignment_id_str)
        )

        if not isinstance(assignment, dict) or "error" in assignment:
            return (
                "Error: fetching assignment. Unavailable: assignment details.\n"
                + observation_context()
            )

        # Get all students in the course
        params = {"enrollment_type[]": "student", "per_page": 100}

        students = await fetch_all_paginated_results(
            canvas_path("courses", course_id, "users"), params
        )

        if not isinstance(students, list) or any(
            not isinstance(student, dict) for student in students
        ):
            return (
                "Error: fetching students. Unavailable: roster denominator.\n"
                + observation_context()
            )

        # Anonymization happens at the client layer (core/client.py) per
        # ENABLE_DATA_ANONYMIZATION (#179)

        # Get submissions for this assignment
        submissions = await fetch_all_paginated_results(
            canvas_path(
                "courses", course_id, "assignments", assignment_id, "submissions"
            ),
            {"per_page": 100, "include[]": ["user"]},
        )

        if not isinstance(submissions, list) or any(
            not isinstance(submission, dict) for submission in submissions
        ):
            return (
                "Error: fetching submissions. Unavailable: all submission fields.\n"
                + observation_context()
            )
        observed_fields = (
            "submitted_at",
            "score",
            "late",
            "missing",
            "excused",
            "workflow_state",
        )
        unavailable = dict.fromkeys(observed_fields, 0)
        roster_ids = {_grading_id(student.get("id")) for student in students}
        if None in roster_ids or len(roster_ids) != len(students):
            return (
                "Error: fetching students. Unavailable: valid unique roster identities.\n"
                + observation_context()
            )
        excluded_submission_count = sum(
            _grading_id(submission.get("user_id")) not in roster_ids
            for submission in submissions
        )
        submissions = [
            submission
            for submission in submissions
            if _grading_id(submission.get("user_id")) in roster_ids
        ]
        if len(
            {_grading_id(submission.get("user_id")) for submission in submissions}
        ) != len(submissions):
            return (
                "Error: fetching submissions. Unavailable: unique submission identities.\n"
                + observation_context()
            )

        # Extract assignment details
        assignment_name = assignment.get("name", "Unknown Assignment")
        raw_due_date = assignment.get("due_at")
        due_date = raw_due_date if isinstance(raw_due_date, str) else None
        raw_points_possible = assignment.get("points_possible")
        points_possible = (
            raw_points_possible
            if _finite_grade_number(raw_points_possible) and raw_points_possible >= 0
            else 0
        )
        is_published = assignment.get("published", False)

        # Format the due date
        due_date_str = (
            "No due date"
            if "due_at" in assignment and raw_due_date is None
            else "Unavailable"
        )
        if due_date:
            due_date_obj = _analytics_timestamp(due_date)
            if due_date_obj:
                due_date_str = format_date(due_date)
                now = datetime.datetime.now(datetime.UTC)
                is_past_due = due_date_obj < now
            else:
                due_date_str = "Unavailable (invalid due date)"
                is_past_due = False
        else:
            is_past_due = False

        # Process submissions
        submission_stats: dict[str, Any] = {
            "total_students": len(students),
            "submitted_count": 0,
            "missing_count": 0,
            "late_count": 0,
            "graded_count": 0,
            "excused_count": 0,
            "scores": [],
            "status_counts": {
                "submitted": 0,
                "unsubmitted": 0,
                "graded": 0,
                "pending_review": 0,
            },
        }

        # Student status tracking
        student_status = []
        missing_students = []
        low_scoring_students: list[tuple[str, int | float, float]] = []
        high_scoring_students: list[tuple[str, int | float, float]] = []

        # Track which students have submissions
        student_ids_with_submissions = set()

        for submission in submissions:
            student_id = _grading_id(submission.get("user_id"))
            student_ids_with_submissions.add(student_id)

            # Find student name
            student_name = "Unknown"
            for student in students:
                if _grading_id(student.get("id")) == student_id:
                    student_name = student.get("name", "Unknown")
                    break

            # Process submission data
            for field in observed_fields:
                if field not in submission:
                    unavailable[field] += 1
                elif field in {"late", "missing", "excused"} and not isinstance(
                    submission[field], bool
                ):
                    unavailable[field] += 1
                elif (
                    field == "score"
                    and submission[field] is not None
                    and not _finite_grade_number(submission[field])
                ):
                    unavailable[field] += 1
                elif (
                    field == "submitted_at"
                    and submission[field] is not None
                    and (
                        not isinstance(submission[field], str)
                        or _analytics_timestamp(submission[field]) is None
                    )
                ):
                    unavailable[field] += 1
                elif field == "workflow_state" and (
                    not isinstance(submission[field], str)
                    or submission[field]
                    not in {"submitted", "unsubmitted", "graded", "pending_review"}
                ):
                    unavailable[field] += 1
            raw_score = submission.get("score")
            score = raw_score if _finite_grade_number(raw_score) else None
            is_submitted = (
                isinstance(submission.get("submitted_at"), str)
                and _analytics_timestamp(submission["submitted_at"]) is not None
            )
            is_late = submission.get("late") is True
            is_missing = submission.get("missing") is True
            is_excused = submission.get("excused") is True
            status = submission.get("workflow_state", "unsubmitted")
            submitted_at = submission.get("submitted_at")

            if submitted_at:
                try:
                    submitted_at = datetime.datetime.fromisoformat(
                        submitted_at.replace("Z", "+00:00")
                    ).strftime("%Y-%m-%d %H:%M")
                except (ValueError, AttributeError):
                    pass

            # Update statistics
            if is_submitted:
                submission_stats["submitted_count"] += 1
            if is_late:
                submission_stats["late_count"] += 1
            if is_missing:
                submission_stats["missing_count"] += 1
                missing_students.append(student_name)
            if is_excused:
                submission_stats["excused_count"] += 1
            if score is not None:
                submission_stats["graded_count"] += 1
                submission_stats["scores"].append(score)

                # Track high/low scoring students
                if points_possible > 0:
                    percentage = (score / points_possible) * 100
                    if percentage < 70:
                        low_scoring_students.append((student_name, score, percentage))
                    if percentage > 90:
                        high_scoring_students.append((student_name, score, percentage))

            # Update status counts
            if isinstance(status, str) and status in submission_stats["status_counts"]:
                submission_stats["status_counts"][status] += 1

            # Add to student status
            student_status.append(
                {
                    "name": student_name,
                    "submitted": is_submitted,
                    "submitted_at": submitted_at,
                    "late": is_late,
                    "missing": is_missing,
                    "excused": is_excused,
                    "score": score,
                    "status": status,
                }
            )

        # Find students with no submissions
        for student in students:
            if _grading_id(student.get("id")) not in student_ids_with_submissions:
                student_name = student.get("name", "Unknown")
                for field in observed_fields:
                    unavailable[field] += 1

                # Add to student status
                student_status.append(
                    {
                        "name": student_name,
                        "submitted": False,
                        "submitted_at": None,
                        "late": False,
                        "missing": None,
                        "excused": False,
                        "score": None,
                        "status": "unsubmitted",
                    }
                )

        # Compute grade statistics
        scores = submission_stats["scores"]
        avg_score = mean(scores) if scores else 0
        median_score = median(scores) if scores else 0

        try:
            std_dev = stdev(scores) if len(scores) > 1 else 0
        except StatisticsError:
            std_dev = 0

        if points_possible > 0:
            avg_percentage = (avg_score / points_possible) * 100
        else:
            avg_percentage = 0

        # Format the output
        course_display = await get_course_code(course_id) or course_identifier
        output = (
            "Assignment Analytics for "
            f"{fence_untrusted_inline(assignment_name, 'assignment name')} "
            f"in Course {course_display}\n\n"
        )

        # Assignment details
        output += "Assignment Details:\n"
        output += f"  Due: {due_date_str}"
        if is_past_due:
            output += " (Past Due)"
        output += "\n"

        shown_points = (
            points_possible
            if _finite_grade_number(raw_points_possible) and raw_points_possible >= 0
            else "Unavailable"
        )
        shown_published = (
            ("Yes" if is_published else "No")
            if isinstance(assignment.get("published"), bool)
            else "Unavailable"
        )
        output += f"  Points Possible: {shown_points}\n"
        output += f"  Published: {shown_published}\n\n"

        # Submission statistics
        output += "Submission Statistics:\n"
        total_students = submission_stats["total_students"]
        submitted = submission_stats["submitted_count"]
        graded = submission_stats["graded_count"]
        missing = submission_stats["missing_count"]
        late = submission_stats["late_count"]

        # Calculate percentages
        submitted_pct = (submitted / total_students * 100) if total_students > 0 else 0
        graded_pct = (graded / total_students * 100) if total_students > 0 else 0
        missing_pct = (missing / total_students * 100) if total_students > 0 else 0
        late_pct = (late / submitted * 100) if submitted > 0 else 0

        output += (
            f"  Submitted: {submitted}/{total_students} ({round(submitted_pct, 1)}%)\n"
        )
        output += f"  Graded: {graded}/{total_students} ({round(graded_pct, 1)}%)\n"
        output += f"  Missing: {missing}/{total_students} ({round(missing_pct, 1)}%)\n"
        if submitted > 0:
            output += (
                f"  Late: {late}/{submitted} ({round(late_pct, 1)}% of submissions)\n"
            )
        output += f"  Excused: {submission_stats['excused_count']}\n\n"

        # Grade statistics
        if scores:
            output += "Grade Statistics:\n"
            shown_avg_percentage = (
                round(avg_percentage, 1) if points_possible > 0 else "unavailable"
            )
            output += f"  Average Score: {round(avg_score, 2)}/{shown_points} ({shown_avg_percentage}%)\n"
            median_percentage = (
                round((median_score / points_possible) * 100, 1)
                if points_possible > 0
                else "unavailable"
            )
            output += f"  Median Score: {round(median_score, 2)}/{points_possible} ({median_percentage}%)\n"
            output += f"  Standard Deviation: {round(std_dev, 2)}\n"

            # High/Low scores
            # Student display names are author-controlled (issue 239).
            if low_scoring_students:
                output += "\nStudents Scoring Below 70%:\n"
                for name, score, percentage in sorted(
                    low_scoring_students, key=lambda x: x[2]
                ):
                    output += f"  {fence_untrusted_inline(name, 'student name')}: {round(score, 1)}/{points_possible} ({round(percentage, 1)}%)\n"

            if high_scoring_students:
                output += "\nStudents Scoring Above 90%:\n"
                for name, score, percentage in sorted(
                    high_scoring_students, key=lambda x: x[2], reverse=True
                ):
                    output += f"  {fence_untrusted_inline(name, 'student name')}: {round(score, 1)}/{points_possible} ({round(percentage, 1)}%)\n"

        # Missing students
        if missing_students:
            output += "\nStudents Missing Submission:\n"
            # Sort alphabetically and show first 10
            for name in sorted(missing_students)[:10]:
                output += f"  {fence_untrusted_inline(name, 'student name')}\n"
            if len(missing_students) > 10:
                output += f"  ...and {len(missing_students) - 10} more\n"

        output += "\nUnavailable field counts (missing or malformed; null score means ungraded):\n"
        for field, count in unavailable.items():
            output += f"  {field}: {count}/{total_students}\n"
        assignment_unavailable = {
            "due_at": "due_at" not in assignment
            or raw_due_date is not None
            and _analytics_timestamp(raw_due_date) is None,
            "points_possible": not _finite_grade_number(raw_points_possible)
            or raw_points_possible < 0,
            "published": not isinstance(assignment.get("published"), bool),
        }
        output += f"  Assignment fields absent: {sum(field not in assignment for field in assignment_unavailable)}/3\n"
        output += f"  Assignment fields unavailable: {sum(assignment_unavailable.values())}/3\n"
        output += f"  No returned submission record: {total_students - len(submissions)}/{total_students} (unknown, not inferred missing)\n"
        output += f"  Submission records outside visible roster: {excluded_submission_count} (excluded)\n"
        output += "Counts describe observed records; unavailable fields are not counted as false or missing.\n"
        output += observation_context()
        return output

    @mcp.tool(
        annotations=ToolAnnotations(destructive_hint=False, idempotent_hint=False)
    )
    @validate_params
    async def create_assignment(
        course_identifier: str | int,
        name: str,
        description: str | None = None,
        submission_types: str | None = None,
        due_at: str | None = None,
        unlock_at: str | None = None,
        lock_at: str | None = None,
        points_possible: float | None = None,
        grading_type: str | None = None,
        published: bool = False,
        assignment_group_id: str | int | None = None,
        peer_reviews: bool = False,
        automatic_peer_reviews: bool = False,
        allowed_extensions: str | None = None,
        allowed_attempts: int | None = None,
        position: int | None = None,
        external_tool_url: str | None = None,
        external_tool_new_tab: bool | None = None,
        omit_from_final_grade: bool | None = None,
        hide_in_gradebook: bool | None = None,
        grading_standard_id: int | None = None,
        annotatable_attachment_id: int | None = None,
    ) -> str:
        """Create a new assignment in a course.

        Args:
            course_identifier: Course code or Canvas ID
            name: Assignment name/title
            description: HTML description
            submission_types: Comma-separated types (online_text_entry, online_url, online_upload, discussion_topic, none, on_paper, external_tool, media_recording, student_annotation)
            due_at: Due date in ISO 8601 format
            unlock_at: Available date in ISO 8601 format
            lock_at: Lock date in ISO 8601 format
            points_possible: Maximum points
            grading_type: points, letter_grade, gpa_scale, pass_fail, percent, not_graded
            published: Whether to publish immediately (default: False)
            assignment_group_id: Assignment group ID
            peer_reviews: Enable peer reviews
            automatic_peer_reviews: Auto-assign peer reviews
            allowed_extensions: Comma-separated file extensions (e.g., "pdf,docx,txt")
            allowed_attempts: Positive attempt limit, or -1 for unlimited.
            position: Positive position within the assignment group.
            external_tool_url: HTTP(S) launch URL for external-tool submission.
            external_tool_new_tab: Open the external tool in a new tab.
            omit_from_final_grade: Exclude this assignment from final grades.
            hide_in_gradebook: Hide this assignment in gradebooks.
            grading_standard_id: Existing grading standard for letter/GPA grades.
            annotatable_attachment_id: Course file for student annotation.
        """
        # Backstop for issue 239: a fenced read result (read→clone) must not
        # publish our provenance markers into live Canvas.
        if contains_fence_markers(name) or (
            description is not None and contains_fence_markers(description)
        ):
            return FENCE_LEAK_ERROR

        course_id = await get_course_id(course_identifier)

        # Validate grading_type if provided
        valid_grading_types = [
            "points",
            "letter_grade",
            "pass_fail",
            "percent",
            "not_graded",
            "gpa_scale",
        ]
        if grading_type and grading_type not in valid_grading_types:
            return f"Invalid grading_type '{grading_type}'. Must be one of: {', '.join(valid_grading_types)}"

        # Validate submission_types if provided
        valid_submission_types = [
            "online_text_entry",
            "online_url",
            "online_upload",
            "discussion_topic",
            "none",
            "on_paper",
            "external_tool",
            "media_recording",
            "student_annotation",
        ]
        submission_types_list = []
        if submission_types:
            submission_types_list = [s.strip() for s in submission_types.split(",")]
            for st in submission_types_list:
                if st not in valid_submission_types:
                    return f"Invalid submission_type '{st}'. Must be one of: {', '.join(valid_submission_types)}"

        # Build assignment data
        assignment_data = {"name": name, "published": published}

        if description:
            assignment_data["description"] = description

        if submission_types_list:
            assignment_data["submission_types"] = submission_types_list

        # Validate and parse date fields
        if due_at:
            parsed_due = parse_date(due_at)
            if not parsed_due:
                return f"Invalid date format for due_at: '{due_at}'. Use ISO 8601 format (e.g., '2026-01-26T23:59:00Z')."
            assignment_data["due_at"] = parsed_due.isoformat()

        if unlock_at:
            parsed_unlock = parse_date(unlock_at)
            if not parsed_unlock:
                return f"Invalid date format for unlock_at: '{unlock_at}'. Use ISO 8601 format (e.g., '2026-01-26T00:00:00Z')."
            assignment_data["unlock_at"] = parsed_unlock.isoformat()

        if lock_at:
            parsed_lock = parse_date(lock_at)
            if not parsed_lock:
                return f"Invalid date format for lock_at: '{lock_at}'. Use ISO 8601 format (e.g., '2026-02-01T23:59:00Z')."
            assignment_data["lock_at"] = parsed_lock.isoformat()

        if points_possible is not None:
            assignment_data["points_possible"] = points_possible

        if grading_type:
            assignment_data["grading_type"] = grading_type

        if assignment_group_id:
            assignment_data["assignment_group_id"] = assignment_group_id

        # Validate peer review settings
        if automatic_peer_reviews and not peer_reviews:
            return "Invalid configuration: automatic_peer_reviews requires peer_reviews=True. Set peer_reviews=True to enable automatic peer review assignment."

        if peer_reviews:
            assignment_data["peer_reviews"] = peer_reviews

        if automatic_peer_reviews:
            assignment_data["automatic_peer_reviews"] = automatic_peer_reviews

        if allowed_extensions:
            extensions_list = [ext.strip() for ext in allowed_extensions.split(",")]
            assignment_data["allowed_extensions"] = extensions_list

        options = _assignment_authoring_options(
            allowed_attempts,
            position,
            external_tool_url,
            external_tool_new_tab,
            omit_from_final_grade,
            hide_in_gradebook,
            grading_standard_id,
            annotatable_attachment_id,
        )
        if isinstance(options, str):
            return options
        effective_types = submission_types_list
        if "external_tool_tag_attributes" in options:
            if effective_types != ["external_tool"]:
                return "Error: external tool options require submission_types='external_tool'."
        if annotatable_attachment_id is not None:
            if "student_annotation" not in (effective_types or []):
                return "Error: annotatable_attachment_id requires student_annotation submission."
            attachment = await make_canvas_request(
                "get",
                canvas_path("courses", course_id, "files", annotatable_attachment_id),
            )
            if (
                not isinstance(attachment, dict)
                or "error" in attachment
                or coerce_canvas_id(attachment.get("id", ""))
                != str(annotatable_attachment_id)
            ):
                return "Error: could not verify the annotation file belongs to this course."
        assignment_data.update(options)

        # Make the API request
        response = await make_canvas_request(
            "post",
            canvas_path("courses", course_id, "assignments"),
            data={"assignment": assignment_data},
        )

        if "error" in response:
            return f"Error creating assignment: {response['error']}"

        # Format success response
        assignment_id = response.get("id")
        assignment_name = response.get("name", name)
        assignment_points = response.get("points_possible")
        assignment_published = response.get("published", False)
        assignment_due = response.get("due_at")
        assignment_types = response.get("submission_types", [])
        html_url = response.get("html_url", "")

        course_display = await get_course_code(course_id) or course_identifier

        result = "✅ Assignment created successfully!\n\n"
        result += f"**{assignment_name}**\n"
        result += f"  Course: {course_display}\n"
        result += f"  Assignment ID: {assignment_id}\n"

        if assignment_points is not None:
            result += f"  Points: {assignment_points}\n"

        if assignment_due:
            result += f"  Due: {format_date(assignment_due)}\n"

        result += f"  Published: {'Yes' if assignment_published else 'No'}\n"

        if assignment_types:
            result += f"  Submission Types: {', '.join(assignment_types)}\n"

        if html_url:
            result += f"  URL: {html_url}\n"

        return result

    @mcp.tool(annotations=ToolAnnotations(destructive_hint=True, idempotent_hint=True))
    @validate_params
    async def update_assignment(
        course_identifier: str | int,
        assignment_id: str | int,
        name: str | None = None,
        description: str | None = None,
        submission_types: str | None = None,
        due_at: str | None = None,
        unlock_at: str | None = None,
        lock_at: str | None = None,
        points_possible: float | None = None,
        grading_type: str | None = None,
        published: bool | None = None,
        assignment_group_id: str | int | None = None,
        peer_reviews: bool | None = None,
        automatic_peer_reviews: bool | None = None,
        allowed_extensions: str | None = None,
        allowed_attempts: int | None = None,
        position: int | None = None,
        external_tool_url: str | None = None,
        external_tool_new_tab: bool | None = None,
        omit_from_final_grade: bool | None = None,
        hide_in_gradebook: bool | None = None,
        grading_standard_id: int | None = None,
        annotatable_attachment_id: int | None = None,
        clear_due_at: bool = False,
        clear_unlock_at: bool = False,
        clear_lock_at: bool = False,
    ) -> str:
        """Update an existing assignment in a course.

        Args:
            course_identifier: Course code or Canvas ID
            assignment_id: Assignment ID to update
            name: New assignment name/title
            description: New HTML description
            submission_types: Comma-separated types (online_text_entry, online_url, online_upload, discussion_topic, none, on_paper, external_tool, media_recording, student_annotation)
            due_at: New due date in ISO 8601 format
            unlock_at: New available date in ISO 8601 format
            lock_at: New lock date in ISO 8601 format
            points_possible: New maximum points
            grading_type: points, letter_grade, gpa_scale, pass_fail, percent, not_graded
            published: Whether to publish the assignment
            assignment_group_id: Assignment group ID to move to
            peer_reviews: Enable peer reviews
            automatic_peer_reviews: Auto-assign peer reviews
            allowed_extensions: Comma-separated file extensions (e.g., "pdf,docx,txt")
            allowed_attempts: Positive attempt limit, or -1 for unlimited.
            position: Positive position within the assignment group.
            external_tool_url: HTTP(S) launch URL for external-tool submission.
            external_tool_new_tab: Open the external tool in a new tab.
            omit_from_final_grade: Exclude this assignment from final grades.
            hide_in_gradebook: Hide this assignment in gradebooks.
            grading_standard_id: Existing grading standard for letter/GPA grades.
            annotatable_attachment_id: Course file for student annotation.
            clear_due_at: Remove the existing due date
            clear_unlock_at: Remove the existing availability date
            clear_lock_at: Remove the existing lock date
        """
        # Backstop for issue 239: never publish our provenance markers.
        if (name is not None and contains_fence_markers(name)) or (
            description is not None and contains_fence_markers(description)
        ):
            return FENCE_LEAK_ERROR

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

        course_id = await get_course_id(course_identifier)

        # Build assignment data - only include fields that are provided
        assignment_data: dict[str, Any] = {}

        if name is not None:
            assignment_data["name"] = name

        if description is not None:
            assignment_data["description"] = description

        # Validate and process submission_types if provided
        if submission_types is not None:
            valid_submission_types = [
                "online_text_entry",
                "online_url",
                "online_upload",
                "discussion_topic",
                "none",
                "on_paper",
                "external_tool",
                "media_recording",
                "student_annotation",
            ]
            submission_types_list = [s.strip() for s in submission_types.split(",")]
            for st in submission_types_list:
                if st not in valid_submission_types:
                    return f"Invalid submission_type '{st}'. Must be one of: {', '.join(valid_submission_types)}"
            assignment_data["submission_types"] = submission_types_list

        # Validate and parse date fields
        if clear_due_at:
            assignment_data["due_at"] = None
        elif due_at is not None:
            parsed_due = parse_date(due_at)
            if not parsed_due:
                return f"Invalid date format for due_at: '{due_at}'. Use ISO 8601 format (e.g., '2026-01-26T23:59:00Z')."
            assignment_data["due_at"] = parsed_due.isoformat()

        if clear_unlock_at:
            assignment_data["unlock_at"] = None
        elif unlock_at is not None:
            parsed_unlock = parse_date(unlock_at)
            if not parsed_unlock:
                return f"Invalid date format for unlock_at: '{unlock_at}'. Use ISO 8601 format (e.g., '2026-01-26T00:00:00Z')."
            assignment_data["unlock_at"] = parsed_unlock.isoformat()

        if clear_lock_at:
            assignment_data["lock_at"] = None
        elif lock_at is not None:
            parsed_lock = parse_date(lock_at)
            if not parsed_lock:
                return f"Invalid date format for lock_at: '{lock_at}'. Use ISO 8601 format (e.g., '2026-02-01T23:59:00Z')."
            assignment_data["lock_at"] = parsed_lock.isoformat()

        if points_possible is not None:
            assignment_data["points_possible"] = points_possible

        # Validate grading_type if provided
        if grading_type is not None:
            valid_grading_types = [
                "points",
                "letter_grade",
                "pass_fail",
                "percent",
                "not_graded",
                "gpa_scale",
            ]
            if grading_type not in valid_grading_types:
                return f"Invalid grading_type '{grading_type}'. Must be one of: {', '.join(valid_grading_types)}"
            assignment_data["grading_type"] = grading_type

        if published is not None:
            assignment_data["published"] = published

        if assignment_group_id is not None:
            assignment_data["assignment_group_id"] = assignment_group_id

        # Validate peer review settings
        if automatic_peer_reviews is True and peer_reviews is False:
            return "Invalid configuration: automatic_peer_reviews requires peer_reviews=True. Set peer_reviews=True to enable automatic peer review assignment."

        if peer_reviews is not None:
            assignment_data["peer_reviews"] = peer_reviews

        if automatic_peer_reviews is not None:
            assignment_data["automatic_peer_reviews"] = automatic_peer_reviews

        if allowed_extensions is not None:
            extensions_list = [ext.strip() for ext in allowed_extensions.split(",")]
            assignment_data["allowed_extensions"] = extensions_list

        options = _assignment_authoring_options(
            allowed_attempts,
            position,
            external_tool_url,
            external_tool_new_tab,
            omit_from_final_grade,
            hide_in_gradebook,
            grading_standard_id,
            annotatable_attachment_id,
        )
        if isinstance(options, str):
            return options
        effective_types = assignment_data.get("submission_types")
        if "external_tool_tag_attributes" in options:
            if effective_types is None:
                current = await make_canvas_request(
                    "get",
                    canvas_path("courses", course_id, "assignments", assignment_id),
                )
                if not isinstance(current, dict) or "error" in current:
                    return "Error: could not verify the current assignment submission type."
                effective_types = current.get("submission_types")
            if effective_types != ["external_tool"]:
                return "Error: external tool options require submission_types='external_tool'."
        if annotatable_attachment_id is not None:
            if effective_types is None:
                current = await make_canvas_request(
                    "get",
                    canvas_path("courses", course_id, "assignments", assignment_id),
                )
                if not isinstance(current, dict) or "error" in current:
                    return "Error: could not verify the current assignment submission type."
                effective_types = current.get("submission_types")
            if "student_annotation" not in (effective_types or []):
                return "Error: annotatable_attachment_id requires student_annotation submission."
            attachment = await make_canvas_request(
                "get",
                canvas_path("courses", course_id, "files", annotatable_attachment_id),
            )
            if (
                not isinstance(attachment, dict)
                or "error" in attachment
                or coerce_canvas_id(attachment.get("id", ""))
                != str(annotatable_attachment_id)
            ):
                return "Error: could not verify the annotation file belongs to this course."
        assignment_data.update(options)

        # Check if there's anything to update
        if not assignment_data:
            return "No fields provided to update. Specify at least one field to modify (e.g., name, description, due_at, points_possible)."

        # Make the API request
        response = await make_canvas_request(
            "put",
            canvas_path("courses", course_id, "assignments", assignment_id),
            data={"assignment": assignment_data},
        )

        if "error" in response:
            return f"Error updating assignment: {response['error']}"

        # Format success response
        updated_name = response.get("name", "")
        updated_points = response.get("points_possible")
        updated_published = response.get("published", False)
        updated_due = response.get("due_at")
        updated_types = response.get("submission_types", [])
        html_url = response.get("html_url", "")

        course_display = await get_course_code(course_id) or course_identifier

        result = "✅ Assignment updated successfully!\n\n"
        result += f"**{updated_name}**\n"
        result += f"  Course: {course_display}\n"
        result += f"  Assignment ID: {assignment_id}\n"

        # Show what was updated
        updated_fields = list(assignment_data.keys())
        result += f"  Updated fields: {', '.join(updated_fields)}\n"

        if updated_points is not None:
            result += f"  Points: {updated_points}\n"

        if updated_due:
            result += f"  Due: {format_date(updated_due)}\n"

        result += f"  Published: {'Yes' if updated_published else 'No'}\n"

        if updated_types:
            result += f"  Submission Types: {', '.join(updated_types)}\n"

        if html_url:
            result += f"  URL: {html_url}\n"

        return result

    @mcp.tool(annotations=ToolAnnotations(destructive_hint=True, idempotent_hint=True))
    @validate_params
    async def delete_assignment_with_confirmation(
        course_identifier: str | int,
        assignment_id: str | int,
        require_name_match: str | None = None,
        allow_deleting_student_work: bool = False,
        confirmation_token: str | None = None,
    ) -> str:
        """Delete an assignment. Two-step: preview first, then confirm with the token.

        Permanent: removes the assignment together with every submission and grade
        attached to it. Canvas may retain a recycle-bin copy depending on admin settings.

        Args:
            course_identifier: Course code or Canvas ID
            assignment_id: Assignment ID to delete
            require_name_match: Only delete if the assignment name matches this string exactly
            allow_deleting_student_work: Must be true when submissions or grades exist
            confirmation_token: Token from the preview call; omit to preview
        """
        course_id = await get_course_id(course_identifier)

        assignment = await make_canvas_request(
            "get", canvas_path("courses", course_id, "assignments", assignment_id)
        )
        if "error" in assignment:
            return f"Error fetching assignment details: {assignment['error']}"

        name = assignment.get("name", "Unknown")
        shown_name = fence_untrusted_inline(name, "assignment name")
        if require_name_match is not None and name != require_name_match:
            return (
                f"❌ Name mismatch — deletion aborted.\n\n"
                f"  Expected: {require_name_match}\n"
                f"  Actual:   {shown_name}"
            )

        has_submissions = bool(assignment.get("has_submitted_submissions"))
        needs_grading = assignment.get("needs_grading_count")
        has_student_work = has_submissions or (
            isinstance(needs_grading, int | float) and needs_grading > 0
        )
        if has_student_work and not allow_deleting_student_work:
            return (
                "Error: this assignment has existing student work. It was not "
                "deleted. Pass allow_deleting_student_work=true only if deleting "
                "its submissions and grades is intentional."
            )
        course_display = await get_course_code(course_id) or course_identifier
        # Everything the preview shows to identify the target is bound, so a
        # due-date or points edit between preview and confirm stops matching.
        fingerprint = _DELETE_ASSIGNMENT_GUARD.fingerprint(
            "delete_assignment_with_confirmation",
            str(course_id),
            str(assignment_id),
            name,
            str(assignment.get("due_at")),
            str(assignment.get("points_possible")),
            str(has_submissions),
            str(needs_grading),
            str(allow_deleting_student_work),
        )
        if not confirmation_token:
            preview = (
                f"Would delete assignment **{shown_name}** from course {course_display}\n"
                f"  Assignment ID: {assignment_id}\n"
                f"  Due: {format_date(assignment.get('due_at'))}\n"
                f"  Points: {assignment.get('points_possible')}\n"
                f"  Submissions: {'yes' if has_submissions else 'none'}"
                f"{f', needs grading: {needs_grading}' if needs_grading is not None else ''}\n"
                f"  Student-work deletion authorized: "
                f"{'yes' if allow_deleting_student_work else 'not needed'}\n"
                "  ⚠️  Deleting an assignment also deletes all of its submissions and grades."
            )
            return preview_with_token(
                _DELETE_ASSIGNMENT_GUARD,
                fingerprint,
                "delete_assignment_with_confirmation",
                preview,
            )
        error = redeem_confirmation(
            _DELETE_ASSIGNMENT_GUARD, confirmation_token, fingerprint
        )
        if error:
            return error

        response = await make_canvas_request(
            "delete", canvas_path("courses", course_id, "assignments", assignment_id)
        )
        if "error" in response:
            return f"Error deleting assignment {shown_name}: {response['error']}"

        return (
            f"✅ Assignment deleted successfully!\n\n"
            f"  **{shown_name}**\n"
            f"  Course: {course_display}\n"
            f"  Assignment ID: {assignment_id}\n"
            f"  Status: deleted (submissions and grades removed with it)"
        )

    @mcp.tool(annotations=ToolAnnotations(destructive_hint=True, idempotent_hint=False))
    @validate_params
    async def bulk_grade_submissions(
        course_identifier: str | int,
        assignment_id: str | int,
        grades: dict[str, Any],
        dry_run: bool = False,
        max_concurrent: int = 5,
        rate_limit_delay: float = 1.0,
    ) -> str:
        """Grade multiple submissions efficiently with concurrent processing.

        Supports both rubric-based and simple point-based grading in batches.
        Each target is read before a single write and inspected afterward.
        Results retain prose and append versioned recovery JSON identifying
        verified, rejected, unknown, and unattempted targets. Authorization
        failures stop later batches; already dispatched writes cannot be undone.
        Comments append, so unknown comment outcomes must be inspected before retry.

        Args:
            course_identifier: Course code or Canvas ID
            assignment_id: Canvas assignment ID
            grades: Dict mapping user_id to {rubric_assessment?, grade?, comment?, expected_attempt?}.
                expected_attempt binds the write to the reviewed attempt; null
                requires an explicitly never-submitted baseline.
                OMIT `comment` unless the instructor explicitly asked for written
                feedback. "Assign grade 8" means the grade ONLY. A comment is
                visible to the student in SpeedGrader, APPENDS a new comment on
                every call rather than replacing the previous one, and cannot be
                un-sent. Never generate a comment that merely restates the grade
                or narrates that grading happened.
            dry_run: If True, validate without submitting (default: False)
            max_concurrent: Max concurrent grading operations (default: 5)
            rate_limit_delay: Delay between batches in seconds (default: 1.0)
        """
        preflight_error = _bulk_grading_preflight(
            grades, max_concurrent, rate_limit_delay
        )
        if preflight_error:
            return preflight_error
        course_id = await get_course_id(course_identifier)
        assignment_id_str = str(assignment_id)
        user_ids = list(grades)
        outcomes: list[dict[str, Any]] = []
        assignment_check: dict[str, Any] = {}
        has_rubric_grades = any(
            info.get("rubric_assessment") for info in grades.values()
        )
        needs_assignment = has_rubric_grades or any(
            str(info.get("grade", "")).strip().endswith("%")
            or str(info.get("grade", "")).lower()
            in {"pass", "complete", "fail", "incomplete"}
            for info in grades.values()
        )
        setup_error = None
        if needs_assignment:
            response = await make_canvas_request(
                "get",
                canvas_path("courses", course_id, "assignments", assignment_id_str),
                params={"include[]": ["rubric", "rubric_settings"]},
            )
            if not isinstance(response, dict) or "error" in response:
                setup_error = "Could not verify assignment grading settings; no assessments were submitted."
            else:
                assignment_check = response
                if (
                    has_rubric_grades
                    and response.get("use_rubric_for_grading") is not True
                ):
                    setup_error = "Rubric is not configured for grading; use_for_grading must be true."
        actor_id = None
        if (
            not dry_run
            and not setup_error
            and any(info.get("comment") for info in grades.values())
        ):
            profile = await make_canvas_request(
                "get", canvas_path("users", "self", "profile")
            )
            if not isinstance(profile, dict) or "error" in profile:
                setup_error = "Could not verify the comment author's identity; no grades were submitted."
            else:
                actor_id = _grading_id(profile.get("id"))
                if actor_id is None:
                    setup_error = "Could not verify the comment author's identity; no grades were submitted."

        async def grade_single_submission(
            user_id: str, info: dict[str, Any]
        ) -> dict[str, Any]:
            """Dispatch once, then observe the persisted grade and optional comment."""
            result: dict[str, Any] = {
                "user_id": user_id,
                "outcome": "unattempted",
                "write_dispatched": False,
                "grade_verified": False,
                "comment_verified": not bool(info.get("comment")),
                "stop_batches": False,
            }
            if dry_run:
                comment_note = (
                    f" AND post this student-visible comment: {info['comment']!r}"
                    if info.get("comment")
                    else ""
                )
                value = (
                    f"{sum(criterion['points'] for criterion in info['rubric_assessment'].values())} rubric points"
                    if info.get("rubric_assessment")
                    else f"{info['grade']} points"
                )
                result.update(
                    reason="dry_run",
                    message=f"DRY RUN: Would grade with {value}{comment_note}",
                )
                return result
            path = canvas_path(
                "courses",
                course_id,
                "assignments",
                assignment_id_str,
                "submissions",
                user_id,
            )
            read_params = {"include[]": ["rubric_assessment", "submission_comments"]}
            try:
                before_response = await make_canvas_request(
                    "get", path, params=read_params
                )
                before = _grading_submission_identity(
                    before_response, assignment_id_str, user_id
                )
                result["stop_batches"] = _grading_policy_failure(before_response)
                if before is None:
                    result["reason"] = "prewrite_identity_unavailable"
                    return result
                attempt = before.get("attempt")
                if not _grading_attempt_available(before):
                    result["reason"] = "prewrite_attempt_unavailable"
                    return result
                result["observed_attempt"] = attempt
                if "expected_attempt" in info and attempt != info["expected_attempt"]:
                    result["reason"] = "reviewed_attempt_changed"
                    return result
                previous_comments = (
                    _grading_comment_ids(before.get("submission_comments"))
                    if info.get("comment")
                    else set()
                )
                if previous_comments is None:
                    result["reason"] = "prewrite_comment_ids_unavailable"
                    return result
                form_data = (
                    build_rubric_assessment_form_data(
                        info["rubric_assessment"], info.get("comment")
                    )
                    if info.get("rubric_assessment")
                    else {"submission[posted_grade]": str(info["grade"])}
                )
                if info.get("comment"):
                    form_data["comment[text_comment]"] = info["comment"]
                result["write_dispatched"] = True
                try:
                    write_response = await make_canvas_request(
                        "put", path, data=form_data, use_form_data=True
                    )
                except Exception:
                    write_response = RequestFailure(
                        "Write dispatch raised an exception.",
                        WriteOutcome.MAY_HAVE_WRITTEN,
                    )
                result["stop_batches"] = _grading_policy_failure(write_response)
                if isinstance(
                    write_response, RequestFailure
                ) and write_response.outcome in {
                    WriteOutcome.REJECTED,
                    WriteOutcome.NOT_DISPATCHED,
                }:
                    result.update(
                        outcome="rejected"
                        if write_response.outcome == WriteOutcome.REJECTED
                        else "unattempted",
                        reason="write_rejected"
                        if write_response.outcome == WriteOutcome.REJECTED
                        else "request_not_dispatched",
                        write_dispatched=write_response.outcome
                        != WriteOutcome.NOT_DISPATCHED,
                    )
                    return result
                result["outcome"] = "unknown"
                result["write_evidence"] = (
                    "uncertain"
                    if isinstance(write_response, dict) and "error" in write_response
                    else "response_received"
                )
                persisted_response = await make_canvas_request(
                    "get", path, params=read_params
                )
                result["readback_observed_at"] = datetime.datetime.now(
                    datetime.UTC
                ).isoformat()
                result["stop_batches"] = result[
                    "stop_batches"
                ] or _grading_policy_failure(persisted_response)
                persisted = _grading_submission_identity(
                    persisted_response, assignment_id_str, user_id
                )
                if persisted is None:
                    result["reason"] = "readback_identity_unavailable"
                    return result
                persisted_attempt = persisted.get("attempt")
                if (
                    not _grading_attempt_available(persisted, readback=True)
                    or persisted_attempt != attempt
                ):
                    result["reason"] = "submission_attempt_changed"
                    return result
                result["grade_verified"] = _persisted_grade_matches(
                    assignment_check, info, persisted
                )
                if info.get("comment"):
                    comments = persisted.get("submission_comments")
                    persisted_ids = _grading_comment_ids(comments)
                    result["comment_verified"] = (
                        persisted_ids is not None
                        and isinstance(comments, list)
                        and any(
                            str(comment["id"]) not in previous_comments
                            and _grading_id(comment.get("author_id")) == actor_id
                            and comment.get("comment") == info["comment"]
                            for comment in comments
                        )
                    )
                if result["grade_verified"] and result["comment_verified"]:
                    result.update(outcome="verified", reason="persisted_state_verified")
                elif not result["grade_verified"]:
                    result["reason"] = (
                        "rubric_grade_unconfirmed"
                        if info.get("rubric_assessment")
                        else "grade_unconfirmed"
                    )
                else:
                    result["reason"] = "comment_unconfirmed"
                return result
            except Exception:
                result.update(
                    outcome="unknown" if result["write_dispatched"] else "unattempted",
                    reason="write_or_readback_exception"
                    if result["write_dispatched"]
                    else "prewrite_exception",
                )
                return result

        total_batches = (len(user_ids) + max_concurrent - 1) // max_concurrent
        lines = [
            f"Bulk Grading {'(DRY RUN) ' if dry_run else ''}for Assignment {assignment_id}",
            f"Course: {await get_course_code(course_id) or course_identifier}",
            f"Total submissions to grade: {len(grades)}",
            f"Concurrent processing: {max_concurrent} per batch",
            f"Total batches: {total_batches}",
        ]
        if setup_error:
            outcomes = [
                {
                    "user_id": uid,
                    "outcome": "unattempted",
                    "reason": "grading_setup_unavailable",
                    "write_dispatched": False,
                }
                for uid in user_ids
            ]
            lines.append("Error: " + setup_error)
        else:
            for offset in range(0, len(user_ids), max_concurrent):
                batch = user_ids[offset : offset + max_concurrent]
                results = await asyncio.gather(
                    *(grade_single_submission(uid, grades[uid]) for uid in batch)
                )
                outcomes.extend(results)
                for result in results:
                    lines.append(
                        f"  User {result['user_id']}: {result.get('message') or result['outcome'] + ' (' + result['reason'] + ')'}"
                    )
                if any(result["stop_batches"] for result in results):
                    outcomes.extend(
                        {
                            "user_id": uid,
                            "outcome": "unattempted",
                            "reason": "stopped_after_authorization_failure",
                            "write_dispatched": False,
                        }
                        for uid in user_ids[offset + max_concurrent :]
                    )
                    lines.append(
                        "Stopped subsequent batches after an authorization or policy failure. Already dispatched items were inspected."
                    )
                    break
                if offset + max_concurrent < len(user_ids):
                    await asyncio.sleep(rate_limit_delay)
        for result in outcomes:
            proposal = grades[result["user_id"]]
            if "expected_attempt" in proposal:
                result["expected_attempt"] = proposal["expected_attempt"]
            result["recovery_action"] = {
                "verified": "do_not_repeat",
                "rejected": "correct_request_and_review",
                "unknown": "inspect_in_canvas_before_retry",
                "unattempted": "review_scope_and_authorization_before_dispatch",
            }[result["outcome"]]
        counts = {
            state: sum(result["outcome"] == state for result in outcomes)
            for state in ("verified", "rejected", "unknown", "unattempted")
        }
        planned = sum(result.get("reason") == "dry_run" for result in outcomes)
        lines.extend(
            [
                f"Total:   {len(grades)}",
                f"Graded:  {counts['verified']}",
                f"Failed:  {len(grades) - counts['verified'] - planned}",
                f"Verified: {counts['verified']}; Rejected: {counts['rejected']}; Unknown: {counts['unknown']}; Unattempted: {counts['unattempted']}",
            ]
        )
        if setup_error:
            lines.insert(0, "Error: Bulk grading was not attempted.")
        if dry_run:
            lines.extend(
                [
                    "DRY RUN MODE: No grades were actually submitted",
                    "Set dry_run=false to apply grades",
                ]
            )
        elif counts["unknown"] or counts["rejected"] or counts["unattempted"]:
            lines.insert(0, "Error: Bulk grading has unresolved outcomes.")
            lines.append(
                "Inspect unknown outcomes in Canvas before retrying. Comments append; never blindly retry a comment. No automatic write retry or rollback was made."
            )
        else:
            lines.append(
                "Bulk Grading Complete: all requested outcomes verified by readback."
            )
        recovery = {
            "schema_version": 1,
            "course_id": str(course_id),
            "assignment_id": assignment_id_str,
            "dry_run": dry_run,
            "counts": counts,
            "items": outcomes,
            "atomic": False,
            "automatic_write_retry": False,
        }
        lines.append("Recovery data (JSON):\n" + json.dumps(recovery, sort_keys=True))
        return "\n".join(lines)
