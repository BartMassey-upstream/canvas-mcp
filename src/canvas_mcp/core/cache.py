"""Course caching system for Canvas API."""

from .client import fetch_all_paginated_results, make_canvas_request
from .credentials import get_request_course_metadata, is_http_request_active
from .logging import log_error, log_info
from .path import canvas_path
from .validation import validate_params

# Global cache for course codes to IDs
course_code_to_id_cache: dict[str, str | None] = {}
id_to_course_code_cache: dict[str, str] = {}


def _caches() -> tuple[dict[str, str | None], dict[str, str]]:
    """Select request-local HTTP state or single-credential stdio state."""
    if is_http_request_active():
        metadata = get_request_course_metadata()
        return metadata.aliases, metadata.labels
    return course_code_to_id_cache, id_to_course_code_cache


def remember_course_labels(courses: list[dict]) -> None:
    """Remember labels without using partial reads to resolve aliases.

    Only a complete course-list refresh establishes alias uniqueness. A
    filtered listing or individual read must not overwrite an ambiguous code.
    """
    _, labels = _caches()
    for course in courses:
        course_id = course.get("id")
        code = course.get("course_code")
        if course_id is not None and isinstance(code, str) and code:
            labels[str(course_id)] = code


async def refresh_course_cache() -> bool:
    """Refresh aliases from the caller's complete course list, atomically."""

    log_info("Refreshing course cache")
    courses = await fetch_all_paginated_results("/courses", {"per_page": 100})

    if isinstance(courses, dict) and "error" in courses:
        log_error("Error building course cache", error=courses.get("error"))
        return False

    if not isinstance(courses, list) or any(not isinstance(c, dict) for c in courses):
        return False

    aliases: dict[str, str | None] = {}
    labels: dict[str, str] = {}
    for course in courses:
        raw_id = course.get("id")
        course_id = str(raw_id)
        course_code = course.get("course_code")
        if raw_id is None or not course_id.isascii() or not course_id.isdigit():
            return False
        if isinstance(course_code, str) and course_code:
            if course_code in aliases and aliases[course_code] != course_id:
                aliases[course_code] = None
            else:
                aliases[course_code] = course_id
            labels[course_id] = course_code

    current_aliases, current_labels = _caches()
    current_aliases.clear()
    current_aliases.update(aliases)
    current_labels.clear()
    current_labels.update(labels)
    if is_http_request_active():
        get_request_course_metadata().loaded = True
    log_info(f"Cached {len(aliases)} course codes")
    return True


def _resolve_alias(code: str, aliases: dict[str, str | None]) -> str | None:
    """Refuse collisions even when Canvas permits both candidate courses."""
    if code in aliases and aliases[code] is None:
        raise ValueError(
            f"Ambiguous course code {code!r}; use a numeric Canvas ID "
            "or an explicit sis_course_id:<id> identifier"
        )
    return aliases.get(code)


@validate_params
async def get_course_id(course_identifier: str | int) -> str:
    """Get course ID from either course code or ID, with caching.

    Args:
        course_identifier: The course identifier, which can be:
                          - A course code (e.g., 'badm_554_120251_246794')
                          - A numeric course ID (as string or int)
                          - A SIS ID format (e.g., 'sis_course_id:xxx')

    Returns:
        The course ID as a string
    """

    # Convert to string for consistent handling
    course_str = str(course_identifier).strip()

    # If it looks like a numeric ID
    if course_str.isascii() and course_str.isdigit():
        return course_str

    # If it's a SIS ID format
    if course_str.startswith("sis_course_id:"):
        if course_str == "sis_course_id:":
            raise ValueError("A sis_course_id identifier must include an ID")
        return course_str

    aliases, _ = _caches()
    resolved = _resolve_alias(course_str, aliases)
    if resolved is not None:
        return resolved

    # Course codes are display values rather than Canvas route identifiers.
    # Resolve every spelling through the caller's course list instead of using
    # punctuation as a proxy for whether a value is a course code.
    if not is_http_request_active() or not get_request_course_metadata().loaded:
        if await refresh_course_cache():
            resolved = _resolve_alias(course_str, aliases)
            if resolved is not None:
                return resolved

    raise ValueError(
        f"Could not resolve course code {course_str!r}; use a numeric Canvas ID "
        "or an explicit sis_course_id:<id> identifier"
    )


async def get_course_code(course_id: str | int) -> str | None:
    """Get course code from ID, with caching."""

    course_id = str(course_id)

    _, labels = _caches()
    if course_id in labels:
        return labels[course_id]

    # Try to refresh cache if it's not there
    if not labels and (
        not is_http_request_active() or not get_request_course_metadata().loaded
    ):
        await refresh_course_cache()
        if course_id in labels:
            return labels[course_id]

    # If we can't find a code, try to fetch the course directly
    response = await make_canvas_request("get", canvas_path('courses', course_id))
    if isinstance(response, dict) and "error" not in response and "course_code" in response:
        code: str | None = response.get("course_code", "")
        if isinstance(code, str) and code:
            labels[course_id] = code
        return code

    # Last resort, return the ID
    return course_id
