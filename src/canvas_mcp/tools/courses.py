"""Course-related MCP tools for Canvas API."""

import html
import json
import re
from html.parser import HTMLParser
from typing import Any

from fastmcp import FastMCP
from mcp.types import ToolAnnotations

from ..core.cache import (
    course_code_to_id_cache,
    get_course_code,
    get_course_id,
    id_to_course_code_cache,
)
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
from ..core.validation import validate_params
from ..core.write_confirmation import (
    ConfirmationGuard,
    preview_with_token,
    redeem_confirmation,
    unconfirmed_write_warning,
)
from .self_identity import _own_roles

# Replacing a syllabus that already has content destroys the only copy Canvas
# keeps -- syllabus_body carries no revision history, unlike a wiki page. So
# that one case takes the same preview->token->confirm path as the delete
# tools; writing into an empty syllabus, or appending, has nothing to lose and
# stays a single call.
_UPDATE_SYLLABUS_GUARD = ConfirmationGuard(
    nothing_done="The syllabus was not changed."
)
_UPDATE_COURSE_DATES_GUARD = ConfirmationGuard(
    nothing_done="The course dates were not changed."
)
_UPDATE_COURSE_SETTINGS_GUARD = ConfirmationGuard(
    nothing_done="The course settings were not changed."
)

_CONFIRMED_COURSE_SETTINGS = frozenset({
    "allow_final_grade_override",
    "hide_final_grades",
    "restrict_student_past_view",
    "restrict_student_future_view",
    "conditional_release",
})
_DEFAULT_DUE_TIME = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d$")


def _course_date_state(course: dict[str, Any]) -> dict[str, Any]:
    return {
        "start_at": course.get("start_at"),
        "end_at": course.get("end_at"),
        "restrict_enrollments_to_course_dates": bool(
            course.get("restrict_enrollments_to_course_dates", False)
        ),
    }


def _normalize_course_date(value: str, field: str) -> tuple[str | None, str | None]:
    parsed = parse_date(value)
    if parsed is None:
        return None, (
            f"Invalid date format for {field}: '{value}'. Use ISO 8601 "
            "format, preferably with Z or an explicit UTC offset."
        )
    return parsed.isoformat(), None


def _dates_match(expected: object, actual: object) -> bool:
    if expected is None or actual is None:
        return expected is actual
    if not isinstance(expected, str) or not isinstance(actual, str):
        return expected == actual
    expected_date = parse_date(expected)
    actual_date = parse_date(actual)
    return bool(expected_date and actual_date and expected_date == actual_date)


def _syllabus_text(body: str) -> str:
    """Visible text of a syllabus body, whitespace-collapsed, for comparison."""
    return " ".join(strip_html_tags(body).split())


class _MediaCollector(HTMLParser):
    """Collect embedded-media elements from a Canvas page body.

    ``source`` is deliberately not collected: it only appears inside
    ``<video>``/``<audio>``, which are already collected, and counting both
    would double-report one player.
    """

    MEDIA_TAGS = frozenset({"img", "iframe", "video", "audio", "embed", "object"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.items: list[dict[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in self.MEDIA_TAGS:
            return
        attr = {k: (v or "") for k, v in attrs}
        self.items.append({
            "tag": tag,
            # <object> uses data=, everything else src=.
            "src": attr.get("src") or attr.get("data") or "",
            "alt": attr.get("alt") or attr.get("title") or "",
        })


def extract_embedded_media(html_content: str) -> list[dict[str, str]]:
    """List the images, videos and embeds in a page body, in document order.

    Canvas page bodies carry course media as ``<img>``/``<iframe>`` markup.
    Any plain-text rendering deletes those tags, and because they are void or
    attribute-only elements the media vanishes without leaving so much as a
    placeholder -- the reader cannot tell anything was there (issue #233).

    Uses stdlib ``HTMLParser``, which is lenient about the unclosed and
    malformed markup real Canvas pages contain. Duplicates (same tag and same
    src) are collapsed, since Canvas often repeats a thumbnail and its link.
    """
    if not html_content:
        return []

    collector = _MediaCollector()
    try:
        collector.feed(html_content)
        collector.close()
    except Exception:  # pragma: no cover - HTMLParser is lenient by design
        return collector.items

    seen: set[tuple[str, str]] = set()
    unique: list[dict[str, str]] = []
    for item in collector.items:
        key = (item["tag"], item["src"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def format_media_inventory(media: list[dict[str, str]]) -> str:
    """Render an embedded-media list as a labelled section, or '' if empty."""
    if not media:
        return ""

    lines = [f"\n\nEmbedded media ({len(media)}):"]
    for item in media:
        src = item["src"] or "(no src attribute)"
        line = f"- {item['tag']}: {src}"
        if item["alt"]:
            line += f" — {item['alt']}"
        lines.append(line)
    return "\n".join(lines)


def strip_html_tags(html_content: str) -> str:
    """Convert HTML to readable plain text.

    Block-level elements (headings, paragraphs, list items, table rows, ``<br>``,
    etc.) become line breaks so adjacent blocks don't run together — e.g.
    ``<h3>Grading</h3><p>Final exam...</p>`` yields ``Grading\nFinal exam...``
    rather than ``GradingFinal exam...``. Inline tags become a space. HTML
    entities are decoded and excess whitespace collapsed (intra-line runs to a
    single space; blank-line runs to at most one).
    """
    if not html_content:
        return ""

    text = html_content

    # Drop <script>/<style> blocks entirely so their JS/CSS contents don't
    # leak into the plain-text output.
    text = re.sub(r'(?is)<(script|style)\b[^>]*>.*?</\1>', '', text)

    # Normalize <br> and block-level boundaries to newlines so content across
    # tag boundaries is separated instead of concatenated.
    text = re.sub(r'(?i)<\s*br\s*/?\s*>', '\n', text)
    text = re.sub(
        r'(?i)</\s*(?:p|div|h[1-6]|li|ul|ol|tr|table|thead|tbody|tfoot|'
        r'section|article|header|footer|blockquote|pre)\s*>',
        '\n',
        text,
    )
    # Separate table cells within a row.
    text = re.sub(r'(?i)</\s*(?:td|th)\s*>', '\t', text)

    # Remove all remaining tags. Use a space so inline tags don't join words.
    text = re.sub(r'<[^>]+>', ' ', text)

    # Decode HTML entities (named, decimal, and hex) via the stdlib — covers
    # smart quotes, dashes, accents, &nbsp;, etc. that Canvas content commonly
    # uses, with no manual entity table to maintain.
    text = html.unescape(text)

    # Collapse intra-line whitespace but preserve line breaks. \xa0 (decoded
    # from &nbsp;) is normalized to a regular space.
    text = re.sub(r'[ \t\xa0]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    text = re.sub(r'\n{3,}', '\n\n', text)

    return text.strip()


def register_course_tools(mcp: FastMCP) -> None:
    """Register all course-related MCP tools."""

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    @validate_params
    async def list_courses(
        include_concluded: bool = False, include_all: bool = False
    ) -> str:
        """List courses for the authenticated user.

        Args:
            include_concluded: Include concluded/past enrollments in the results.
            include_all: Include all enrollments instead of only current active ones.
        """

        role = get_config().canvas_role
        params: dict[str, Any] = {"include[]": ["term"], "per_page": 100}
        if role != "creator":
            params["include[]"].extend(["teachers", "total_students"])

        if not include_all:
            # Scope to the user's *current* enrollments. enrollment_state="active"
            # is Canvas's canonical "current" signal; the course state[] filter
            # below cannot distinguish current from past at institutions that
            # never flip finished courses to workflow_state="completed".
            params["enrollment_state"] = "active"
            # Educators and creators keep teacher-only scoping. Students and
            # the "all" profile see every active enrollment, which is what a
            # shared tool should return — the old unconditional teacher filter
            # returned nothing for students.
            if role in ("creator", "educator"):
                params["enrollment_type"] = "teacher"

        if include_concluded:
            params["state[]"] = ["available", "completed"]
        else:
            params["state[]"] = ["available"]

        courses = await fetch_all_paginated_results("/courses", params)

        if isinstance(courses, dict) and "error" in courses:
            return f"Error fetching courses: {courses['error']}"

        if not courses:
            return "No courses found."

        # Refresh our caches with the course data
        for course in courses:
            course_id = str(course.get("id"))
            course_code = course.get("course_code")

            if course_code and course_id:
                course_code_to_id_cache[course_code] = course_id
                id_to_course_code_cache[course_id] = course_code

        courses_info = []
        for course in courses:
            course_id = course.get("id")
            name = course.get("name", "Unnamed course")
            code = course.get("course_code", "No code")

            # Canvas already ships the caller's own enrollments[] on /courses
            # (no include[] needed); dropping it used to force callers toward
            # roster tools they have no permission for (issue #171).
            roles = _own_roles(course)
            role_line = f"Your role: {', '.join(roles)}\n" if roles else ""

            # Emphasize code in the output
            courses_info.append(
                f"Code: {code}\nName: {name}\nID: {course_id}\n{role_line}"
            )

        return "Courses:\n\n" + "\n".join(courses_info)

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    @validate_params
    async def get_course_details(course_identifier: str | int) -> str:
        """Get detailed information about a specific course.

        Args:
            course_identifier: Course code or Canvas ID
        """
        course_id = await get_course_id(course_identifier)

        response = await make_canvas_request(
            "get",
            canvas_path('courses', course_id),
            params={"include[]": "term"},
        )

        if "error" in response:
            return f"Error fetching course details: {response['error']}"

        # Update our caches with the course data
        if "id" in response and "course_code" in response:
            course_code_to_id_cache[response["course_code"]] = str(response["id"])
            id_to_course_code_cache[str(response["id"])] = response["course_code"]

        details = [
            f"Code: {response.get('course_code', 'N/A')}",
            f"Name: {response.get('name', 'N/A')}",
            f"Workflow State: {response.get('workflow_state', 'N/A')}",
            f"Start Date: {format_date(response.get('start_at'))}",
            f"End Date: {format_date(response.get('end_at'))}",
            "Course Dates Enforced: "
            + (
                "Yes"
                if response.get("restrict_enrollments_to_course_dates")
                else "No; term dates normally govern access"
            ),
            f"Time Zone: {response.get('time_zone', 'N/A')}",
            f"Default View: {response.get('default_view', 'N/A')}",
            f"Public: {response.get('is_public', False)}",
            f"Blueprint: {response.get('blueprint', False)}"
        ]

        term = response.get("term")
        if isinstance(term, dict):
            details.extend([
                f"Term: {term.get('name', 'N/A')}",
                f"Term Start Date: {format_date(term.get('start_at'))}",
                f"Term End Date: {format_date(term.get('end_at'))}",
            ])

        # Surface the caller's own role. Say so explicitly when there is none —
        # silence reads as "unknown" and sends agents to roster tools they cannot
        # use (issue #171).
        roles = _own_roles(response)
        if roles:
            details.append(f"Your role: {', '.join(roles)}")
        else:
            details.append("Your role: You have no enrollment in this course")

        # Prefer to show course code in the output
        course_display = response.get("course_code", course_identifier)
        return f"Course Details for {course_display}:\n\n" + "\n".join(details)

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    @validate_params
    async def get_syllabus(course_identifier: str | int,
                           output_format: str = "text",
                           max_chars: int | None = None) -> str:
        """Get the complete Canvas Syllabus tab content for a course, untruncated.

        Unlike get_course_content_overview (which returns only a ~1000-char
        preview), this returns the full syllabus body so later sections such as
        grading policies, weighting, and final-exam details remain accessible.

        Args:
            course_identifier: Course code or Canvas ID
            output_format: "text" (plain text, default), "html" (raw HTML body),
                or "both" (plain text followed by raw HTML)
            max_chars: Optional positive cap on the returned characters per
                section. When exceeded, the content is truncated with an explicit
                "[truncated...]" marker. Defaults to None (no truncation).
        """
        # Validate inputs before any network I/O so bad arguments fail fast.
        fmt = (output_format or "text").lower()
        if fmt not in ("text", "html", "both"):
            return (
                f"Error: invalid output_format '{output_format}'. "
                "Use 'text', 'html', or 'both'."
            )
        if max_chars is not None and max_chars <= 0:
            return "Error: max_chars must be a positive integer (or omitted for no limit)."

        course_id = await get_course_id(course_identifier)

        response = await make_canvas_request(
            "get",
            canvas_path('courses', course_id),
            params={"include[]": "syllabus_body"},
        )

        if "error" in response:
            return f"Error fetching syllabus: {response['error']}"

        course_display = response.get("course_code", course_identifier)
        syllabus_body = response.get("syllabus_body") or ""

        if not syllabus_body.strip():
            return f"No syllabus content found for course {course_display}."

        def _maybe_truncate(text: str) -> str:
            if max_chars is not None and len(text) > max_chars:
                return text[:max_chars] + f"\n\n...[truncated at {max_chars} characters]"
            return text

        # Section headers only help disambiguate when both formats are present.
        labeled = fmt == "both"
        sections = [f"Syllabus for Course {course_display}:"]

        # Syllabus bodies are course-authored free text (issue 239): fence them
        # so embedded directives arrive marked as data, not instructions.
        if fmt in ("text", "both"):
            plain_text = _maybe_truncate(strip_html_tags(syllabus_body))
            sections.append(
                ("\n--- Plain Text ---\n" if labeled else "\n")
                + fence_untrusted(plain_text, "course syllabus")
            )

        if fmt in ("html", "both"):
            raw_html = _maybe_truncate(syllabus_body)
            sections.append(
                ("\n--- Raw HTML ---\n" if labeled else "\n")
                + fence_untrusted(raw_html, "course syllabus")
            )

        return "\n".join(sections)

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    @validate_params
    async def get_course_content_overview(course_identifier: str | int,
                                        include_pages: bool = True,
                                        include_modules: bool = True,
                                        include_syllabus: bool = True) -> str:
        """Get a comprehensive overview of course content including pages, modules, and syllabus.

        Args:
            course_identifier: Course code or Canvas ID
            include_pages: Include pages information (default: True)
            include_modules: Include modules and their items (default: True)
            include_syllabus: Include syllabus content (default: True)
        """
        course_id = await get_course_id(course_identifier)

        overview_sections = []

        # Get course details for context
        course_response = await make_canvas_request("get", canvas_path('courses', course_id))
        if "error" not in course_response:
            course_name = course_response.get("name", "Unknown Course")
            overview_sections.append(f"Course: {course_name}")

        # Get pages if requested
        if include_pages:
            pages = await fetch_all_paginated_results(canvas_path('courses', course_id, 'pages'), {"per_page": 100})
            if isinstance(pages, list):
                published_pages = [p for p in pages if p.get("published", False)]
                unpublished_pages = [p for p in pages if not p.get("published", False)]
                front_pages = [p for p in pages if p.get("front_page", False)]

                pages_summary = [
                    "\nPages Summary:",
                    f"  Total Pages: {len(pages)}",
                    f"  Published: {len(published_pages)}",
                    f"  Unpublished: {len(unpublished_pages)}",
                    f"  Front Pages: {len(front_pages)}"
                ]

                if published_pages:
                    pages_summary.append("\nRecent Published Pages:")
                    # Sort by updated_at and show first 5
                    sorted_pages = sorted(published_pages,
                                        key=lambda x: x.get("updated_at", ""),
                                        reverse=True)
                    for page in sorted_pages[:5]:
                        title = page.get("title", "Untitled")
                        updated = format_date(page.get("updated_at"))
                        # Page titles are author-controlled where page editing
                        # is open to students (issue 239).
                        pages_summary.append(
                            f"    {fence_untrusted(title, 'page title')} "
                            f"(Updated: {updated})"
                        )

                overview_sections.append("\n".join(pages_summary))

        # Get modules if requested
        if include_modules:
            modules = await fetch_all_paginated_results(canvas_path('courses', course_id, 'modules'), {"per_page": 100})
            if isinstance(modules, list):
                modules_summary = [
                    "\nModules Summary:",
                    f"  Total Modules: {len(modules)}"
                ]

                # Count module items by type across all modules
                item_type_counts: dict[str, int] = {}
                total_items = 0

                for module in modules[:10]:  # Limit to first 10 modules to avoid too many API calls
                    module_id = module.get("id")
                    if module_id:
                        items = await fetch_all_paginated_results(
                            canvas_path('courses', course_id, 'modules', module_id, 'items'),
                            {"per_page": 100}
                        )
                        if isinstance(items, list):
                            total_items += len(items)
                            for item in items:
                                item_type = item.get("type", "Unknown")
                                item_type_counts[item_type] = item_type_counts.get(item_type, 0) + 1

                modules_summary.append(f"  Total Items Analyzed: {total_items}")
                if item_type_counts:
                    modules_summary.append("  Item Types:")
                    for item_type, count in sorted(item_type_counts.items()):
                        modules_summary.append(f"    {item_type}: {count}")

                # Show module structure for first few modules
                if modules:
                    modules_summary.append("\nModule Structure (first 3):")
                    for module in modules[:3]:
                        name = module.get("name", "Unnamed")
                        state = module.get("state", "unknown")
                        # Module names are instructor-authored (issue 239).
                        modules_summary.append(
                            f"    {fence_untrusted_inline(name, 'module name')} (Status: {state})"
                        )

                overview_sections.append("\n".join(modules_summary))

        # Get syllabus content if requested
        if include_syllabus:
            # Fetch the course details with syllabus_body included
            course_with_syllabus = await make_canvas_request(
                "get",
                canvas_path('courses', course_id),
                params={"include[]": "syllabus_body"}
            )

            if "error" not in course_with_syllabus:
                syllabus_body = course_with_syllabus.get('syllabus_body', '')

                if syllabus_body:
                    # Clean the HTML content
                    clean_syllabus = strip_html_tags(syllabus_body)

                    # For overview, limit to first 1000 characters
                    if len(clean_syllabus) > 1000:
                        clean_syllabus = clean_syllabus[:1000] + "..."

                    indented = "\n".join(
                        [f"  {line}" for line in clean_syllabus.split('\n') if line.strip()]
                    )
                    syllabus_summary = [
                        "\nSyllabus Content:",
                        # Course-authored free text (issue 239): fence it.
                        fence_untrusted(indented, "course syllabus (preview)")
                    ]

                    overview_sections.append("\n".join(syllabus_summary))
                else:
                    overview_sections.append("\nSyllabus Content: No syllabus content found")
            else:
                overview_sections.append("\nSyllabus Content: Error fetching syllabus")
        # Try to get the course code for display
        course_display = await get_course_code(course_id) or course_identifier
        result = f"Content Overview for Course {course_display}:" + "\n".join(overview_sections)

        return result


def register_shared_content_tools(mcp: FastMCP) -> None:
    """Register shared content tools (pages, module items) for both students and educators."""

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    @validate_params
    async def list_pages(course_identifier: str | int,
                        sort: str | None = "title",
                        order: str | None = "asc",
                        search_term: str | None = None,
                        published: bool | None = None) -> str:
        """List pages for a specific course.

        Args:
            course_identifier: Course code or Canvas ID
            sort: Sort by 'title', 'created_at', or 'updated_at'
            order: 'asc' or 'desc'
            search_term: Filter pages containing this term
            published: Filter by published status (None for all)
        """
        course_id = await get_course_id(course_identifier)

        params: dict[str, Any] = {"per_page": 100}

        if sort:
            params["sort"] = sort
        if order:
            params["order"] = order
        if search_term:
            params["search_term"] = search_term
        if published is not None:
            params["published"] = published

        pages = await fetch_all_paginated_results(canvas_path('courses', course_id, 'pages'), params)

        if isinstance(pages, dict) and "error" in pages:
            return f"Error fetching pages: {pages['error']}"

        if not pages:
            return f"No pages found for course {course_identifier}."

        pages_info = []
        for page in pages:
            url = page.get("url", "No URL")
            title = page.get("title", "Untitled page")
            published_status = "Published" if page.get("published", False) else "Unpublished"
            is_front_page = page.get("front_page", False)
            updated_at = format_date(page.get("updated_at"))

            front_page_indicator = " (Front Page)" if is_front_page else ""

            # Page titles are author-controlled where page editing is open to
            # students (issue 239) — fenced in listings too.
            pages_info.append(
                f"URL: {url}\n"
                f"Title{front_page_indicator}:\n{fence_untrusted(title, 'page title')}\n"
                f"Status: {published_status}\nUpdated: {updated_at}\n"
            )

        course_display = await get_course_code(course_id) or course_identifier
        return f"Pages for Course {course_display}:\n\n" + "\n".join(pages_info)

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    @validate_params
    async def get_page_content(course_identifier: str | int, page_url_or_id: str) -> str:
        """Get the full content body of a specific page.

        Returns the page's raw HTML body untruncated, followed by an inventory
        of any embedded media (images, videos, iframes) with their source URLs,
        so media is reported explicitly rather than left for the reader to spot
        in the markup.

        Args:
            course_identifier: Course code or Canvas ID
            page_url_or_id: Page URL slug or page ID
        """
        course_id = await get_course_id(course_identifier)

        response = await make_canvas_request("get", canvas_path('courses', course_id, 'pages', page_url_or_id))

        if "error" in response:
            return f"Error fetching page content: {response['error']}"

        title = response.get("title", "Untitled")
        body = response.get("body", "")
        published = response.get("published", False)

        if not body:
            return "This page has no content. Its title:\n" + fence_untrusted(
                title, "page title"
            )

        course_display = await get_course_code(course_id) or course_identifier
        status = "Published" if published else "Unpublished"

        # Title, body, AND the media inventory derived from the body are all
        # page-author-controlled (issue 239) — every one of them goes inside
        # a single fence; only our own framing stays outside. The inventory is
        # computed from the raw body BEFORE fencing, so spoof-neutralization
        # can never alter what it sees.
        untrusted = (
            f"Title: {title}\n\n{body}"
            + format_media_inventory(extract_embedded_media(body))
        )
        return (
            f"Page Content for page '{page_url_or_id}' in Course {course_display} ({status}):\n\n"
            + fence_untrusted(untrusted, "page title, body, and media inventory")
        )

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    @validate_params
    async def get_page_details(course_identifier: str | int, page_url_or_id: str) -> str:
        """Get a specific page's metadata plus a short text preview.

        Returns settings (status, timestamps, editor, editing roles) and a
        PLAIN-TEXT preview capped at 500 characters. The preview drops all
        markup, so embedded media is listed separately rather than silently
        disappearing. For the full body, including media markup, use
        get_page_content.

        Args:
            course_identifier: Course code or Canvas ID
            page_url_or_id: Page URL slug or page ID
        """
        course_id = await get_course_id(course_identifier)

        response = await make_canvas_request("get", canvas_path('courses', course_id, 'pages', page_url_or_id))

        if "error" in response:
            return f"Error fetching page details: {response['error']}"

        title = response.get("title", "Untitled")
        url = response.get("url", "N/A")
        body = response.get("body", "")
        created_at = format_date(response.get("created_at"))
        updated_at = format_date(response.get("updated_at"))
        published = response.get("published", False)
        front_page = response.get("front_page", False)
        locked_for_user = response.get("locked_for_user", False)
        editing_roles = response.get("editing_roles", "")

        # Handle last edited by user info
        last_edited_by = response.get("last_edited_by", {})
        editor_name_raw = last_edited_by.get("display_name", "Unknown") if last_edited_by else "Unknown"
        # Editor display name is author-controlled where names are editable (issue 239).
        editor_name = fence_untrusted_inline(editor_name_raw, "editor display name")

        # Build a TEXT PREVIEW of the body. Both lossy steps below must announce
        # themselves: a silent strip made 4 embedded videos vanish from a real
        # course page with no trace in the output (issue #233), and a bare "..."
        # does not tell the reader a fixed budget was hit.
        #
        # strip_html_tags (not a bare `<[^>]+>` regex) because it also drops
        # <script>/<style> CONTENTS. The naive form deletes only the tags, which
        # promotes script text into what reads as page prose.
        media = extract_embedded_media(body)
        if body:
            body_clean = strip_html_tags(body).strip()
            if len(body_clean) > 500:
                body_clean = body_clean[:500] + "\n...[text preview truncated at 500 characters]"
        else:
            body_clean = "No content"

        status_info = []
        if published:
            status_info.append("Published")
        else:
            status_info.append("Unpublished")

        if front_page:
            status_info.append("Front Page")

        if locked_for_user:
            status_info.append("Locked")

        course_display = await get_course_code(course_id) or course_identifier

        result = f"Page Details for Course {course_display}:\n\n"
        result += f"URL: {url}\n"
        result += f"Status: {', '.join(status_info)}\n"
        result += f"Created: {created_at}\n"
        result += f"Updated: {updated_at}\n"
        result += f"Last Edited By: {editor_name}\n"
        result += f"Editing Roles: {editing_roles or 'Not specified'}\n"

        # Title, text preview, and media src URLs are all page-author-
        # controlled (issue 239): one fence around the lot, our framing
        # outside it.
        untrusted = f"Title: {title}\n\nContent Preview (text only, truncated):\n{body_clean}"
        if media:
            untrusted += (
                f"\n\n{len(media)} embedded media item(s) are present but not shown "
                "in this text preview — use get_page_content for the full HTML:"
            )
            for item in media:
                untrusted += f"\n- {item['tag']}: {item['src'] or '(no src attribute)'}"

        result += "\n" + fence_untrusted(
            untrusted, "page title, text preview, and media inventory"
        )

        return result

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    @validate_params
    async def get_front_page(course_identifier: str | int) -> str:
        """Get the front page content for a course.

        Args:
            course_identifier: Course code or Canvas ID
        """
        course_id = await get_course_id(course_identifier)

        response = await make_canvas_request("get", canvas_path('courses', course_id, 'front_page'))

        if "error" in response:
            return f"Error fetching front page: {response['error']}"

        title = response.get("title", "Untitled")
        body = response.get("body", "")
        updated_at = format_date(response.get("updated_at"))

        if not body:
            return "The course front page has no content. Its title:\n" + fence_untrusted(
                title, "front page title"
            )

        # Try to get the course code for display
        course_display = await get_course_code(course_id) or course_identifier
        # Title and body are both page-author-controlled (issue 239).
        return (
            f"Front Page for Course {course_display} (Updated: {updated_at}):\n\n"
            + fence_untrusted(f"Title: {title}\n\n{body}", "front page title and body")
        )

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    @validate_params
    async def list_module_items(course_identifier: str | int,
                               module_id: str | int,
                               include_content_details: bool = True,
                               search_term: str | None = None) -> str:
        """List items within a specific module, including pages.

        Args:
            course_identifier: Course code or Canvas ID
            module_id: The module ID
            include_content_details: Include additional content details (default: True)
            search_term: Match part of the module item title.
        """
        course_id = await get_course_id(course_identifier)

        params: dict[str, Any] = {"per_page": 100}
        if search_term is not None:
            params["search_term"] = search_term
        if include_content_details:
            params["include[]"] = ["content_details"]

        items = await fetch_all_paginated_results(
            canvas_path('courses', course_id, 'modules', module_id, 'items'), params
        )

        if isinstance(items, dict) and "error" in items:
            return f"Error fetching module items: {items['error']}"

        if not items:
            return f"No items found in module {module_id}."

        # Get module details for context
        module_response = await make_canvas_request(
            "get", canvas_path('courses', course_id, 'modules', module_id)
        )

        module_name = "Unknown Module"
        if "error" not in module_response:
            module_name = module_response.get("name", "Unknown Module")

        course_display = await get_course_code(course_id) or course_identifier
        # Module name and item titles are instructor-authored (issue 239).
        result = (
            f"Module Items for {fence_untrusted_inline(module_name, 'module name')} "
            f"in Course {course_display}:\n\n"
        )

        for item in items:
            item_id = item.get("id")
            title = item.get("title", "Untitled")
            item_type = item.get("type", "Unknown")
            content_id = item.get("content_id")
            url = item.get("url", "")
            external_url = item.get("external_url", "")
            published = item.get("published", False)

            result += f"Item: {fence_untrusted_inline(title, 'module item title')}\n"
            result += f"Type: {item_type}\n"
            result += f"ID: {item_id}\n"
            if content_id:
                result += f"Content ID: {content_id}\n"
            if url:
                result += f"URL: {url}\n"
            if external_url:
                result += f"External URL: {external_url}\n"
            result += f"Published: {'Yes' if published else 'No'}\n\n"

        return result


def register_educator_course_tools(mcp: FastMCP) -> None:
    """Register course tools that write, so need an instructor-scoped token.

    Kept out of ``register_course_tools`` on purpose: that group is shared with
    the student profile, and a student token cannot write a syllabus. Offering
    the tool there would only produce 401s and widen the student profile's
    write surface for no gain.
    """

    @mcp.tool(annotations=ToolAnnotations(destructive_hint=False, idempotent_hint=True))
    @validate_params
    async def update_course_home_page(
        course_identifier: str | int,
        default_view: str,
    ) -> dict[str, Any]:
        """Choose the course home: feed, wiki, modules, assignments, or syllabus.

        Wiki uses the existing front page selected with update_page_settings.
        The change is verified by reading the course back.
        """
        if default_view not in {"feed", "wiki", "modules", "assignments", "syllabus"}:
            return {"error": "default_view must be feed, wiki, modules, assignments, or syllabus."}
        course_id = await get_course_id(course_identifier)
        if default_view == "wiki":
            front_page = await make_canvas_request(
                "get", canvas_path("courses", course_id, "front_page")
            )
            if not isinstance(front_page, dict) or "error" in front_page:
                return {"error": "Select an existing course front page with update_page_settings first."}
        response = await make_canvas_request(
            "put", canvas_path("courses", course_id), data={"course": {"default_view": default_view}}
        )
        if not isinstance(response, dict) or "error" in response:
            return {"error": "Canvas did not accept the home-page update."}
        verified = await make_canvas_request("get", canvas_path("courses", course_id))
        if not isinstance(verified, dict) or "error" in verified or verified.get("default_view") != default_view:
            return {"warning": "The home-page update could not be verified.", "requested_default_view": default_view}
        return {"course_id": str(course_id), "default_view": default_view, "verified": True}

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    @validate_params
    async def get_course_settings(
        course_identifier: str | int,
    ) -> dict[str, Any]:
        """Read course availability dates, term dates, and course settings.

        The result identifies whether course or term dates govern access.
        Section dates can still override both and are not changed here.

        Args:
            course_identifier: Course code or Canvas ID.
        """
        course_id = await get_course_id(course_identifier)
        course = await make_canvas_request(
            "get",
            canvas_path("courses", course_id),
            params={"include[]": "term"},
        )
        if not isinstance(course, dict):
            return {"error": "Canvas returned an invalid course response."}
        if "error" in course:
            return {"error": f"Could not read the course: {course['error']}"}

        settings = await make_canvas_request(
            "get", canvas_path("courses", course_id, "settings")
        )
        if not isinstance(settings, dict):
            return {"error": "Canvas returned an invalid course settings response."}
        if "error" in settings:
            return {"error": f"Could not read course settings: {settings['error']}"}

        term = course.get("term")
        if not isinstance(term, dict):
            term = {}
        restrict_dates = bool(course.get("restrict_enrollments_to_course_dates"))
        return {
            "course_id": str(course_id),
            "course_code": course.get("course_code"),
            "workflow_state": course.get("workflow_state"),
            "default_view": course.get("default_view"),
            "course_dates": {
                "start_at": course.get("start_at"),
                "start_at_display": format_date(course.get("start_at")),
                "end_at": course.get("end_at"),
                "end_at_display": format_date(course.get("end_at")),
                "restrict_enrollments_to_course_dates": restrict_dates,
            },
            "term": {
                "id": term.get("id"),
                "name": term.get("name"),
                "start_at": term.get("start_at"),
                "start_at_display": format_date(term.get("start_at")),
                "end_at": term.get("end_at"),
                "end_at_display": format_date(term.get("end_at")),
            },
            "effective_date_source": "course" if restrict_dates else "term",
            "settings": settings,
            "notes": [
                "Section-specific dates may override course and term dates.",
                "Institutional policy may prevent teachers from editing course availability.",
            ],
        }

    @mcp.tool(
        annotations=ToolAnnotations(destructive_hint=True, idempotent_hint=True)
    )
    @validate_params
    async def update_course_dates(
        course_identifier: str | int,
        start_at: str | None = None,
        end_at: str | None = None,
        clear_start_at: bool = False,
        clear_end_at: bool = False,
        restrict_enrollments_to_course_dates: bool | None = None,
        confirmation_token: str | None = None,
    ) -> str | dict[str, Any]:
        """Preview and confirm changes to course availability dates.

        Dates use ISO 8601. A date and its matching clear flag are mutually
        exclusive. Every change is two-step because course availability can
        remove student access and is not restored by a course-content export.

        Args:
            course_identifier: Course code or Canvas ID.
            start_at: New ISO 8601 course start date.
            end_at: New ISO 8601 course end date.
            clear_start_at: Explicitly remove the course start date.
            clear_end_at: Explicitly remove the course end date.
            restrict_enrollments_to_course_dates: Make course dates govern
                enrollment access rather than term dates.
            confirmation_token: Single-use token from the preview call.
        """
        if start_at is not None and clear_start_at:
            return "Error: provide start_at or clear_start_at, not both."
        if end_at is not None and clear_end_at:
            return "Error: provide end_at or clear_end_at, not both."
        if (
            start_at is None
            and end_at is None
            and not clear_start_at
            and not clear_end_at
            and restrict_enrollments_to_course_dates is None
        ):
            return "Error: no course date fields were provided to update."

        updates: dict[str, Any] = {}
        if clear_start_at:
            updates["start_at"] = None
        elif start_at is not None:
            normalized, error = _normalize_course_date(start_at, "start_at")
            if error:
                return f"Error: {error}"
            updates["start_at"] = normalized

        if clear_end_at:
            updates["end_at"] = None
        elif end_at is not None:
            normalized, error = _normalize_course_date(end_at, "end_at")
            if error:
                return f"Error: {error}"
            updates["end_at"] = normalized

        if restrict_enrollments_to_course_dates is not None:
            updates["restrict_enrollments_to_course_dates"] = (
                restrict_enrollments_to_course_dates
            )

        course_id = await get_course_id(course_identifier)
        current = await make_canvas_request(
            "get",
            canvas_path("courses", course_id),
            params={"include[]": "term"},
        )
        if not isinstance(current, dict):
            return "Error: Canvas returned an invalid course response."
        if "error" in current:
            return f"Error fetching current course dates: {current['error']}"

        current_state = _course_date_state(current)
        proposed_state = {**current_state, **updates}
        setting_a_date = any(
            updates.get(field) is not None for field in ("start_at", "end_at")
        )
        if setting_a_date and not proposed_state["restrict_enrollments_to_course_dates"]:
            return (
                "Error: Canvas ignores course end dates, and may ignore start "
                "dates, unless restrict_enrollments_to_course_dates is true. "
                "Enable it in the same request."
            )

        proposed_start = proposed_state.get("start_at")
        proposed_end = proposed_state.get("end_at")
        if proposed_start and proposed_end:
            parsed_start = parse_date(str(proposed_start))
            parsed_end = parse_date(str(proposed_end))
            if parsed_start and parsed_end and parsed_end <= parsed_start:
                return "Error: end_at must be later than start_at."

        changed = {
            key: value
            for key, value in updates.items()
            if (
                not _dates_match(current_state.get(key), value)
                if key in ("start_at", "end_at")
                else current_state.get(key) != value
            )
        }
        if not changed:
            return "Error: the requested course date settings are already in effect."

        fingerprint = _UPDATE_COURSE_DATES_GUARD.fingerprint(
            "update_course_dates",
            str(course_id),
            json.dumps(current_state, sort_keys=True),
            json.dumps(updates, sort_keys=True),
        )
        course_display = current.get("course_code") or course_identifier
        if not confirmation_token:
            lines = [
                f"Would update availability dates for course {course_display}.",
                f"Current: {json.dumps(current_state, sort_keys=True)}",
                f"Requested: {json.dumps(updates, sort_keys=True)}",
                "This can change when students can access the course.",
                "Section-specific dates may still override these values.",
            ]
            if updates.get("restrict_enrollments_to_course_dates") is False:
                lines.append(
                    "Canvas may remove stored course dates when date restriction is disabled."
                )
            return preview_with_token(
                _UPDATE_COURSE_DATES_GUARD,
                fingerprint,
                "update_course_dates",
                "\n".join(lines),
                action="update the course dates",
            )

        error = redeem_confirmation(
            _UPDATE_COURSE_DATES_GUARD, confirmation_token, fingerprint
        )
        if error:
            return error

        response = await make_canvas_request(
            "put",
            canvas_path("courses", course_id),
            data={"course": updates},
        )
        if not isinstance(response, dict):
            return "Error: Canvas returned an invalid course update response."
        if "error" in response:
            return f"Error updating course dates: {response['error']}"

        verify = await make_canvas_request(
            "get", canvas_path("courses", course_id), params={"include[]": "term"}
        )
        if not isinstance(verify, dict) or "error" in verify:
            return unconfirmed_write_warning(
                "the course date update",
                {"Course": course_display, "Requested": updates},
                "Open Course Settings in Canvas and verify the availability dates.",
            )

        mismatches = []
        for key, expected in updates.items():
            actual = verify.get(key)
            matches = (
                _dates_match(expected, actual)
                if key in ("start_at", "end_at")
                else actual == expected
            )
            if not matches:
                mismatches.append({"field": key, "expected": expected, "actual": actual})
        if mismatches:
            return {
                "error": (
                    "Canvas accepted the request but did not store every course "
                    "date setting. Institutional policy or SIS management may "
                    "prevent teacher edits."
                ),
                "course_id": str(course_id),
                "mismatches": mismatches,
            }

        return {
            "updated": True,
            "course_id": str(course_id),
            "course_code": verify.get("course_code") or course_display,
            "course_dates": _course_date_state(verify),
            "note": "Section-specific dates may override these course dates.",
        }

    @mcp.tool(
        annotations=ToolAnnotations(destructive_hint=True, idempotent_hint=True)
    )
    @validate_params
    async def update_course_settings(
        course_identifier: str | int,
        allow_final_grade_override: bool | None = None,
        allow_student_discussion_topics: bool | None = None,
        allow_student_forum_attachments: bool | None = None,
        allow_student_discussion_editing: bool | None = None,
        allow_student_organized_groups: bool | None = None,
        allow_student_discussion_reporting: bool | None = None,
        allow_student_anonymous_discussion_topics: bool | None = None,
        filter_speed_grader_by_student_group: bool | None = None,
        hide_final_grades: bool | None = None,
        hide_distribution_graphs: bool | None = None,
        hide_sections_on_course_users_page: bool | None = None,
        lock_all_announcements: bool | None = None,
        usage_rights_required: bool | None = None,
        restrict_student_past_view: bool | None = None,
        restrict_student_future_view: bool | None = None,
        show_announcements_on_home_page: bool | None = None,
        home_page_announcement_limit: int | None = None,
        syllabus_course_summary: bool | None = None,
        default_due_time: str | None = None,
        conditional_release: bool | None = None,
        confirmation_token: str | None = None,
    ) -> str | dict[str, Any]:
        """Update the documented Canvas course-settings whitelist.

        Access restrictions, grade visibility/override, and conditional
        release changes require preview and confirmation. Other settings are
        written immediately. Every write is verified by reading settings back.

        Args:
            course_identifier: Course code or Canvas ID.
            allow_final_grade_override: Allow final-grade overrides.
            allow_student_discussion_topics: Let students create topics.
            allow_student_forum_attachments: Let students attach discussion files.
            allow_student_discussion_editing: Let students edit/delete replies.
            allow_student_organized_groups: Let students organize groups.
            allow_student_discussion_reporting: Let students report content.
            allow_student_anonymous_discussion_topics: Let students create
                anonymous discussion topics.
            filter_speed_grader_by_student_group: Filter SpeedGrader by group.
            hide_final_grades: Hide totals in student grade summaries.
            hide_distribution_graphs: Hide grade distribution graphs.
            hide_sections_on_course_users_page: Hide other sections from students.
            lock_all_announcements: Disable announcement comments.
            usage_rights_required: Require file copyright/license information.
            restrict_student_past_view: Hide the course after its end date.
            restrict_student_future_view: Hide the course before its start date.
            show_announcements_on_home_page: Show announcements on the home page.
            home_page_announcement_limit: Number of home-page announcements.
            syllabus_course_summary: Show assignments/events on the syllabus.
            default_due_time: UI default in HH:MM:SS format, or ``inherit``.
                This does not change existing assignment due dates.
            conditional_release: Enable conditional learning paths.
            confirmation_token: Token required for sensitive changes.
        """
        candidates: dict[str, Any] = {
            "allow_final_grade_override": allow_final_grade_override,
            "allow_student_discussion_topics": allow_student_discussion_topics,
            "allow_student_forum_attachments": allow_student_forum_attachments,
            "allow_student_discussion_editing": allow_student_discussion_editing,
            "allow_student_organized_groups": allow_student_organized_groups,
            "allow_student_discussion_reporting": allow_student_discussion_reporting,
            "allow_student_anonymous_" "discussion_topics": (
                allow_student_anonymous_discussion_topics
            ),
            "filter_speed_grader_by_student_group": (
                filter_speed_grader_by_student_group
            ),
            "hide_final_grades": hide_final_grades,
            "hide_distribution_graphs": hide_distribution_graphs,
            "hide_sections_on_course_users_page": hide_sections_on_course_users_page,
            "lock_all_announcements": lock_all_announcements,
            "usage_rights_required": usage_rights_required,
            "restrict_student_past_view": restrict_student_past_view,
            "restrict_student_future_view": restrict_student_future_view,
            "show_announcements_on_home_page": show_announcements_on_home_page,
            "home_page_announcement_limit": home_page_announcement_limit,
            "syllabus_course_summary": syllabus_course_summary,
            "default_due_time": default_due_time,
            "conditional_release": conditional_release,
        }
        updates = {key: value for key, value in candidates.items() if value is not None}
        if not updates:
            return "Error: no course settings were provided to update."
        if home_page_announcement_limit is not None and home_page_announcement_limit < 1:
            return "Error: home_page_announcement_limit must be at least 1."
        if default_due_time is not None and not (
            default_due_time == "inherit" or _DEFAULT_DUE_TIME.fullmatch(default_due_time)
        ):
            return (
                "Error: default_due_time must be 'inherit' or a 24-hour time "
                "in HH:MM:SS format."
            )

        course_id = await get_course_id(course_identifier)
        current = await make_canvas_request(
            "get", canvas_path("courses", course_id, "settings")
        )
        if not isinstance(current, dict):
            return "Error: Canvas returned an invalid course settings response."
        if "error" in current:
            return f"Error fetching current course settings: {current['error']}"

        changed = {key: value for key, value in updates.items() if current.get(key) != value}
        if not changed:
            return "Error: the requested course settings are already in effect."
        current_values = {key: current.get(key) for key in changed}
        needs_confirmation = bool(_CONFIRMED_COURSE_SETTINGS.intersection(changed))
        fingerprint = _UPDATE_COURSE_SETTINGS_GUARD.fingerprint(
            "update_course_settings",
            str(course_id),
            json.dumps(current_values, sort_keys=True),
            json.dumps(changed, sort_keys=True),
        )

        if needs_confirmation and not confirmation_token:
            course_display = await get_course_code(course_id) or course_identifier
            preview = (
                f"Would update sensitive settings for course {course_display}.\n"
                f"Current: {json.dumps(current_values, sort_keys=True)}\n"
                f"Requested: {json.dumps(changed, sort_keys=True)}\n"
                "These changes can affect student access, grade visibility, "
                "or conditional-release behavior."
            )
            return preview_with_token(
                _UPDATE_COURSE_SETTINGS_GUARD,
                fingerprint,
                "update_course_settings",
                preview,
                action="update the course settings",
            )
        if needs_confirmation:
            error = redeem_confirmation(
                _UPDATE_COURSE_SETTINGS_GUARD, confirmation_token or "", fingerprint
            )
            if error:
                return error

        response = await make_canvas_request(
            "put", canvas_path("courses", course_id, "settings"), data=changed
        )
        if not isinstance(response, dict):
            return "Error: Canvas returned an invalid course settings update response."
        if "error" in response:
            return f"Error updating course settings: {response['error']}"

        verify = await make_canvas_request(
            "get", canvas_path("courses", course_id, "settings")
        )
        if not isinstance(verify, dict) or "error" in verify:
            return unconfirmed_write_warning(
                "the course settings update",
                {"Course ID": course_id, "Requested": changed},
                "Open Course Settings in Canvas and verify the changed fields.",
            )
        mismatches = [
            {"field": key, "expected": value, "actual": verify.get(key)}
            for key, value in changed.items()
            if verify.get(key) != value
        ]
        if mismatches:
            return {
                "error": (
                    "Canvas accepted the request but did not store every course "
                    "setting. The setting may be unavailable or institution-managed."
                ),
                "course_id": str(course_id),
                "mismatches": mismatches,
            }
        return {
            "updated": True,
            "course_id": str(course_id),
            "changed_settings": changed,
        }

    # idempotent_hint=False: a replace converges, but mode="append"/"prepend"
    # adds the same block again on every repeat, and the hint is per-tool (a
    # host retrying a timed-out call cannot know which mode was used).
    @mcp.tool(annotations=ToolAnnotations(destructive_hint=True, idempotent_hint=False))
    @validate_params
    async def update_syllabus(course_identifier: str | int,
                              syllabus_body: str,
                              mode: str = "replace",
                              confirmation_token: str | None = None) -> str:
        """Set the Canvas Syllabus tab content for a course.

        Canvas keeps no revision history for the syllabus, so replacing a
        syllabus that already has content is a two-step call: call without
        confirmation_token to get a preview plus a single-use token, show the
        preview to the educator, and only after they approve it call again with
        the token and identical arguments. Writing into an empty syllabus, or appending/prepending,
        destroys nothing and takes a single call.

        Args:
            course_identifier: Course code or Canvas ID
            syllabus_body: HTML for the syllabus. Canvas stores this as HTML;
                plain text is accepted but renders without formatting.
            mode: "replace" (default) swaps the whole body, "append" adds to
                the end of the existing body, "prepend" adds to the start.
            confirmation_token: Token from the preview call. Only needed when
                replacing a syllabus that already has content; a token that is
                supplied is always checked, in every mode.
        """
        normalized_mode = (mode or "replace").lower()
        if normalized_mode not in ("replace", "append", "prepend"):
            return (
                f"Error: invalid mode '{mode}'. "
                "Use 'replace', 'append', or 'prepend'."
            )

        # Backstop for issue 239: get_syllabus fences the body it returns, so a
        # model round-tripping that output would otherwise write our own
        # provenance markers into the course.
        if contains_fence_markers(syllabus_body):
            return FENCE_LEAK_ERROR

        if not syllabus_body.strip():
            if normalized_mode == "replace":
                return (
                    "Error: syllabus_body is empty. To clear a syllabus "
                    "deliberately, pass a body such as '<p></p>'."
                )
            return f"Error: syllabus_body is empty, so there is nothing to {normalized_mode}."

        course_id = await get_course_id(course_identifier)

        current = await make_canvas_request(
            "get",
            canvas_path('courses', course_id),
            params={"include[]": "syllabus_body"},
        )
        if "error" in current:
            return f"Error fetching current syllabus: {current['error']}"

        existing_body = current.get("syllabus_body") or ""
        course_display = current.get("course_code", course_identifier)
        has_existing = bool(existing_body.strip())

        if normalized_mode == "append":
            new_body = f"{existing_body}\n{syllabus_body}" if has_existing else syllabus_body
        elif normalized_mode == "prepend":
            new_body = f"{syllabus_body}\n{existing_body}" if has_existing else syllabus_body
        else:
            new_body = syllabus_body

        # Only a replace over existing content is unrecoverable, so only that
        # path demands a token. But ANY call that bears a token is a
        # confirmation attempt and is checked, whatever the mode: an invalid
        # token used to be ignored on append/prepend and the write went through
        # anyway (GHSA-hmr8 side finding).
        if confirmation_token is not None or (normalized_mode == "replace" and has_existing):
            # existing_body is bound too, not just the replacement: the
            # preview shows the content about to be destroyed, and the token
            # promises it "stops matching if the target changes in the
            # meantime". Without it, a co-teacher editing the syllabus between
            # preview and confirm loses work the confirmer never saw. The mode
            # and the supplied body are bound as well, so a token previewed
            # for one call cannot be redeemed by a different call that happens
            # to produce the same result.
            fingerprint = _UPDATE_SYLLABUS_GUARD.fingerprint(
                "update_syllabus",
                str(course_id),
                normalized_mode,
                syllabus_body,
                existing_body,
                new_body,
            )
            if confirmation_token is None:
                # Show what will land, not just its length: the person
                # approving has to be able to read the change (GHSA-hmr8).
                preview = (
                    f"Would REPLACE the entire syllabus of course {course_display}.\n"
                    f"  Replacing {len(existing_body)} characters of existing "
                    f"content with {len(new_body)}.\n"
                    f"  Canvas keeps no revision history for the syllabus, so "
                    f"the current content cannot be recovered.\n\n"
                    f"  Current syllabus (plain text):\n"
                    f"{fence_untrusted(strip_html_tags(existing_body), 'course syllabus')}\n\n"
                    f"  Proposed replacement (plain text):\n"
                    f"{fence_untrusted(strip_html_tags(new_body), 'proposed syllabus')}\n\n"
                    f"  Exact HTML that will be written (plain text hides link "
                    f"and embed destinations):\n"
                    f"{fence_untrusted(new_body, 'proposed syllabus HTML')}"
                )
                return preview_with_token(
                    _UPDATE_SYLLABUS_GUARD,
                    fingerprint,
                    "update_syllabus",
                    preview,
                    action="replace the syllabus",
                )
            error = redeem_confirmation(
                _UPDATE_SYLLABUS_GUARD, confirmation_token, fingerprint
            )
            if error:
                return error

        response = await make_canvas_request(
            "put",
            canvas_path('courses', course_id),
            data={"course": {"syllabus_body": new_body}},
        )
        if "error" in response:
            return f"Error updating syllabus: {response['error']}"

        # Canvas answers 200 on this PUT without echoing syllabus_body, and it
        # drops the field entirely for a token lacking manage_course_content.
        # Read it back rather than trusting the status code.
        verify = await make_canvas_request(
            "get",
            canvas_path('courses', course_id),
            params={"include[]": "syllabus_body"},
        )
        saved_body = verify.get("syllabus_body") or "" if "error" not in verify else None

        if saved_body is None:
            return unconfirmed_write_warning(
                "the syllabus update",
                {
                    "Course": course_display,
                    "Mode": normalized_mode,
                    "Canvas response": "accepted the write but the read-back failed",
                },
                "Open the course Syllabus tab in Canvas to check whether it saved.",
            )

        # Compare the visible text, not the markup. Canvas returns HTML it
        # rewrote server-side: institutional themes (DesignPlus on the Canvas
        # this was tested against) inject <link>/<script> tags into every
        # syllabus body, and the sanitizer drops attributes such as
        # rel="noopener". Byte equality therefore fails on writes that
        # succeeded perfectly, which would train the reader to ignore the
        # warning. Text containment still catches the failure that matters --
        # a token without manage_course_content leaves the old body in place,
        # so the text just written is absent.
        sent_text = _syllabus_text(new_body)
        stored_text = _syllabus_text(saved_body)
        if sent_text and sent_text not in stored_text:
            return unconfirmed_write_warning(
                "the syllabus update",
                {
                    "Course": course_display,
                    "Mode": normalized_mode,
                    "Sent": f"{len(new_body)} characters",
                    "Stored by Canvas": f"{len(saved_body)} characters",
                },
                "Canvas accepted the request but the syllabus does not contain "
                "what was sent. This usually means the token lacks permission to "
                "edit the syllabus. Check the Syllabus tab.",
            )

        verb = {
            "replace": "Replaced",
            "append": "Appended to",
            "prepend": "Prepended to",
        }[normalized_mode]
        lines = [
            f"✅ {verb} the syllabus of course {course_display}.\n",
            f"  Mode: {normalized_mode}",
            f"  Syllabus is now {len(saved_body)} characters",
        ]
        if sent_text:
            lines.append("  Verified by reading the syllabus back from Canvas")
        else:
            # Markup with no visible text (an image or embed on its own) gives
            # the containment check nothing to look for, so say that rather
            # than claiming a verification that never ran.
            lines.append(
                "  ⚠️  Not verified: the body sent has no visible text to look "
                "for in the read-back. Check the Syllabus tab."
            )
        if saved_body.strip() != new_body.strip():
            lines.append(
                "  Note: Canvas stored a rewritten copy of the HTML (institutional "
                "theme injection or sanitizing). The text sent is present."
            )
        return "\n".join(lines)
