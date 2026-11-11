"""Accessibility-related MCP tools for Canvas API.

This module provides tools to fetch and parse UFIXIT accessibility reports,
format violations for easy consumption, and optionally apply automated fixes
for common accessibility issues.
"""

import json
import re
from bisect import bisect_left
from collections.abc import Iterator
from typing import Any

from fastmcp import FastMCP
from mcp.types import ToolAnnotations

from ..core.cache import get_course_id
from ..core.client import fetch_all_paginated_results, make_canvas_request
from ..core.config import get_config
from ..core.html import HTMLTag, iter_html_tags, remove_html_tags, strip_html_tags
from ..core.path import canvas_path
from ..core.untrusted_content import (
    fence_untrusted,
    fence_untrusted_fields,
    strip_fence_markers,
)
from ..core.validation import validate_params


def register_accessibility_tools(mcp: FastMCP) -> None:
    """Register accessibility MCP tools.

    The built-in scanner (scan_course_content_accessibility /
    fix_accessibility_issues) needs no Canvas add-on and is always registered.
    The UFIXIT/UDOIT report pipeline is registered only when
    ACCESSIBILITY_CHECKERS includes ``ufixit`` (the default) — see #325.
    """
    if "ufixit" in get_config().accessibility_checkers:
        _register_ufixit_tools(mcp)
    _register_builtin_scanner_tools(mcp)


def _register_ufixit_tools(mcp: FastMCP) -> None:
    """UFIXIT/UDOIT report pipeline — requires the add-on on the instance."""

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    @validate_params
    async def fetch_ufixit_report(
        course_identifier: str | int,
        page_title: str = "UFIXIT"
    ) -> str:
        """Fetch UFIXIT accessibility report from Canvas course pages.

        Args:
            course_identifier: Course code or Canvas ID
            page_title: Title of the UFIXIT report page (default: "UFIXIT")
        """
        course_id = await get_course_id(course_identifier)

        # First, try to find the page by title
        pages = await fetch_all_paginated_results(
            canvas_path('courses', course_id, 'pages'),
            {"per_page": 100, "search_term": page_title}
        )

        if isinstance(pages, dict) and "error" in pages:
            return json.dumps({"error": f"Error fetching pages: {pages['error']}"})

        if not pages:
            return json.dumps({
                "error": f"No page found with title containing '{page_title}'",
                "suggestion": "Try specifying a different page_title parameter"
            })

        # Get the first matching page
        target_page = pages[0]
        page_url = target_page.get("url")

        if not page_url:
            return json.dumps({"error": "Found page but no URL available"})

        # Fetch the full page content
        page_response = await make_canvas_request(
            "get",
            canvas_path('courses', course_id, 'pages', page_url)
        )

        if "error" in page_response:
            return json.dumps({"error": f"Error fetching page content: {page_response['error']}"})

        # The UFIXIT page title and body are authored by whoever can edit that
        # page (issue 239) and flow into the model here. Fence them at this
        # output boundary. This is safe for the write-back path: the fenced
        # JSON is consumed only by parse_ufixit_violations (which strips the
        # markers before parsing) — fix_accessibility_issues re-fetches page
        # bodies from Canvas independently and never sees this output, so no
        # fence can be PUT back into a live page.
        return json.dumps({
            "page_title": fence_untrusted(page_response.get("title", "Unknown"), "page title"),
            "page_url": page_url,
            "page_id": page_response.get("page_id"),
            "body": fence_untrusted(page_response.get("body", ""), "page body"),
            "updated_at": page_response.get("updated_at"),
            "course_id": course_id
        })

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    @validate_params
    async def parse_ufixit_violations(report_json: str) -> str:
        """Parse UFIXIT report content to extract accessibility violations.

        Args:
            report_json: JSON string from fetch_ufixit_report
        """
        try:
            report = json.loads(report_json)
        except json.JSONDecodeError:
            return json.dumps({"error": "Invalid JSON input"})

        if "error" in report:
            return json.dumps(report)

        body = report.get("body", "")
        if not body:
            return json.dumps({"error": "Report body is empty"})

        # fetch_ufixit_report fences the body for the model; strip the marker
        # lines before HTML parsing so extraction sees the real content.
        body = strip_fence_markers(body)

        violations = _extract_violations_from_html(body)

        # `location` is a raw author-controlled HTML line copied from the report
        # body (issue 239). Fence it for the model-facing JSON only — this tool
        # returns to the caller and is never fed into fix_accessibility_issues
        # (which re-fetches page bodies from Canvas), so no fence reaches a
        # write-back. type/severity/description are fixed lookups, not fenced.
        # Fence AFTER the summary (which counts by type/severity, not location).
        summary = _generate_violation_summary(violations)
        fence_untrusted_fields(violations, {"location": "content excerpt"})

        return json.dumps({
            "summary": summary,
            "violations": violations,
            "report_metadata": {
                "page_title": report.get("page_title"),
                "updated_at": report.get("updated_at"),
                "course_id": report.get("course_id")
            }
        })

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    @validate_params
    async def format_accessibility_summary(violations_json: str) -> str:
        """Format parsed violations into a human-readable summary.

        Args:
            violations_json: JSON string from parse_ufixit_violations
        """
        try:
            data = json.loads(violations_json)
        except json.JSONDecodeError:
            return json.dumps({"error": "Invalid JSON input"})

        if "error" in data:
            return json.dumps(data)

        summary = data.get("summary", {})
        violations = data.get("violations", [])
        metadata = data.get("report_metadata", {})

        # Build formatted output
        lines = ["# Accessibility Report Summary", ""]

        # Metadata
        if metadata.get("page_title"):
            lines.append(f"**Report**: {metadata['page_title']}")
        if metadata.get("updated_at"):
            lines.append(f"**Last Updated**: {metadata['updated_at']}")
        lines.append("")

        # Summary statistics
        lines.append("## Overview")
        lines.append(f"- **Total Violations**: {summary.get('total_violations', 0)}")
        lines.append("")

        if summary.get("by_severity"):
            lines.append("### By Severity")
            for severity, count in summary["by_severity"].items():
                lines.append(f"- {severity.title()}: {count}")
            lines.append("")

        if summary.get("by_wcag_criterion"):
            lines.append("### By WCAG Criterion")
            for criterion, count in sorted(summary["by_wcag_criterion"].items()):
                lines.append(f"- WCAG {criterion}: {count}")
            lines.append("")

        # Detailed violations
        if violations:
            lines.append("## Detailed Violations")
            lines.append("")

            for i, violation in enumerate(violations[:20], 1):  # Limit to first 20
                lines.append(f"### {i}. {violation.get('type', 'Unknown Issue')}")
                if violation.get("wcag_criterion"):
                    lines.append(f"**WCAG**: {violation['wcag_criterion']}")
                if violation.get("severity"):
                    lines.append(f"**Severity**: {violation['severity']}")
                if violation.get("description"):
                    lines.append(f"**Description**: {violation['description']}")
                if violation.get("location"):
                    # location is already fenced by parse_ufixit_violations
                    # (this tool's documented input), so it is emitted as-is to
                    # avoid double-fencing (issue 239).
                    lines.append(f"**Location**: {violation['location']}")
                if violation.get("remediation"):
                    lines.append(f"**How to Fix**: {violation['remediation']}")
                lines.append("")

            if len(violations) > 20:
                lines.append(f"*...and {len(violations) - 20} more violations*")

        return "\n".join(lines)



class _AccessibilityValidationError(ValueError):
    """A safe, locally generated accessibility validation error."""


def _accessibility_types(value: str) -> list[str]:
    """Validate the supported content types before contacting Canvas."""
    types = [part.strip() for part in value.split(",")]
    if not types or any(t not in {"pages", "assignments"} for t in types):
        raise _AccessibilityValidationError("content_types must contain only pages and/or assignments")
    return list(dict.fromkeys(types))


def _accessibility_body(data: Any, field: str) -> str:
    """Accept an explicit null as empty, but reject absent or invalid content."""
    if not isinstance(data, dict) or "error" in data or field not in data:
        raise _AccessibilityValidationError("Canvas content is missing or unreadable")
    value = data[field]
    if value is None:
        return ""
    if not isinstance(value, str):
        raise _AccessibilityValidationError("Canvas content has an invalid body")
    return value


def _accessibility_detail(data: Any, field: str, expected: dict[str, Any]) -> str:
    """Validate both the identity and the body of a fresh Canvas detail."""
    body = _accessibility_body(data, field)
    if any(key not in data or str(data[key]) != str(value) for key, value in expected.items()):
        raise _AccessibilityValidationError("Canvas detail identity is missing or mismatched")
    return body


async def _accessibility_sources(course_id: Any, types: list[str], *, details: bool) -> list[dict[str, Any]]:
    """Collect and validate every source before a fix can be written."""
    items: list[dict[str, Any]] = []
    for kind in types:
        params: dict[str, Any] = {"per_page": 100}
        if kind == "pages":
            params["include[]"] = "body"
        rows = await fetch_all_paginated_results(canvas_path('courses', course_id, kind), params)
        if not isinstance(rows, list):
            raise _AccessibilityValidationError(f"Could not read {kind} listing")
        for row in rows:
            if not isinstance(row, dict):
                raise _AccessibilityValidationError(f"Invalid {kind} listing entry")
            page = kind == "pages"
            identity = row.get("url" if page else "id")
            title = row.get("title" if page else "name")
            if (not isinstance(identity, (str, int)) or isinstance(identity, bool)
                    or not str(identity).strip() or not isinstance(title, str) or not title.strip()):
                raise _AccessibilityValidationError(f"Invalid identity or title in {kind} listing")
            if page and (not isinstance(row.get("page_id"), (str, int))
                         or isinstance(row.get("page_id"), bool) or not str(row.get("page_id")).strip()):
                raise _AccessibilityValidationError("Invalid identity in pages listing")
            field = "body" if page else "description"
            body = _accessibility_body(row, field)
            endpoint = canvas_path('courses', course_id, kind, identity)
            expected = {"url": identity, "page_id": row["page_id"]} if page else {"id": identity}
            if details:
                detail = await make_canvas_request("get", endpoint)
                body = _accessibility_detail(detail, field, expected)
            items.append({"type": "page" if page else "assignment", "id": identity,
                          "content_id": row.get("page_id") if page else identity,
                          "title": title, "body": body, "endpoint": endpoint, "expected": expected,
                          "field": field, "body_field": "wiki_page[body]" if page else "assignment[description]"})
    return items


def _register_builtin_scanner_tools(mcp: FastMCP) -> None:
    """Add-on-free scanner over Canvas page/assignment HTML."""

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    @validate_params
    async def scan_course_content_accessibility(
        course_identifier: str | int,
        content_types: str = "pages,assignments"
    ) -> str:
        """Scan pages and assignments for basic accessibility issues.

        This heuristic scan is not a complete WCAG audit. Unreadable content
        is an error, never a clean scan. Other content types are unsupported.

        Args:
            course_identifier: Course code or Canvas ID.
            content_types: Comma-separated pages and/or assignments.
        """
        try:
            types = _accessibility_types(content_types)
            course_id = await get_course_id(course_identifier)
            items = await _accessibility_sources(course_id, types, details=False)
        except _AccessibilityValidationError as exc:
            return json.dumps({"error": str(exc)})
        except Exception:
            return json.dumps({"error": "Could not read accessibility sources"})

        all_issues: list[dict[str, Any]] = []
        for item in items:
            all_issues.extend(_check_content_accessibility(
                item["body"], content_type=item["type"],
                content_id=item["content_id"], content_title=item["title"]
            ))
        summary = _generate_violation_summary(all_issues)
        fence_untrusted_fields(all_issues, {
            "content_title": "content title", "location": "content excerpt",
            "description": "accessibility description",
        })
        return json.dumps({"summary": summary, "issues": all_issues, "scanned_types": types})

    @mcp.tool(annotations=ToolAnnotations(destructive_hint=True, idempotent_hint=True))
    @validate_params
    async def fix_accessibility_issues(
        course_identifier: str | int,
        fix_types: str = "th_scope,low_contrast,legacy_designplus,redundant_alt_prefix",
        content_types: str = "pages",
        dry_run: bool = True
    ) -> str:
        """Preview or apply accessibility fixes to pages and assignments.

        Args:
            course_identifier: Course code or Canvas ID.
            fix_types: Comma-separated th_scope, low_contrast,
                legacy_designplus, and redundant_alt_prefix.
            content_types: Comma-separated pages and/or assignments.
            dry_run: Preview only by default; set False to apply changes.
        """
        try:
            content = _accessibility_types(content_types)
            types = [t.strip() for t in fix_types.split(",")]
            if any(t not in {"th_scope", "low_contrast", "legacy_designplus", "redundant_alt_prefix"} for t in types):
                raise _AccessibilityValidationError("Unsupported fix type")
            course_id = await get_course_id(course_identifier)
            items = await _accessibility_sources(course_id, content, details=True)
        except _AccessibilityValidationError as exc:
            return json.dumps({"error": str(exc)})
        except Exception:
            return json.dumps({"error": "Could not read accessibility sources"})
        results: list[dict[str, Any]] = []
        for item in items:
            html = original = item["body"]
            changes: list[str] = []
            for name, fixer, label in (
                ("th_scope", _fix_th_scope, "<th> elements fixed"),
                ("low_contrast", _fix_orange_contrast, "header(s) fixed"),
                ("legacy_designplus", _fix_legacy_designplus, "class(es) migrated"),
                ("redundant_alt_prefix", _fix_redundant_alt_prefix, "alt text(s) fixed"),
            ):
                if name in types:
                    html, count = fixer(html)
                    if count:
                        changes.append(f"{name}: {count} {label}")
            if not changes or html == original:
                continue
            result: dict[str, Any] = {"title": fence_untrusted(item["title"], "content title"),
                                      "type": item["type"], "changes": changes, "applied": False}
            if not dry_run:
                try:
                    current = _accessibility_detail(
                        await make_canvas_request("get", item["endpoint"]),
                        item["field"], item["expected"])
                except Exception:
                    result["error"] = "Could not verify source; no write attempted"
                else:
                    if current != original:
                        result["error"] = "Source changed since it was read; no write attempted"
                    else:
                        try:
                            response = await make_canvas_request(
                                "put", item["endpoint"], data={item["body_field"]: html}, use_form_data=True)
                            if not isinstance(response, dict) or "error" in response:
                                result["error"] = "Write failed or was unconfirmed; it may have happened; no retry attempted"
                            else:
                                stored = _accessibility_detail(
                                    await make_canvas_request("get", item["endpoint"]),
                                    item["field"], item["expected"])
                                if stored == html:
                                    result["applied"] = True
                                else:
                                    result["error"] = "Readback differs; write is unconfirmed and may have happened; no retry attempted"
                        except Exception:
                            result["error"] = "Write or readback failed; write is unconfirmed and may have happened; no retry attempted"
            results.append(result)
        summary = {"course_id": course_id, "dry_run": dry_run, "items_scanned": len(items),
                   "items_with_fixes": len(results), "fix_types_applied": types}
        return json.dumps({"summary": summary, "results": results})


def _style_declarations(style: str) -> list[tuple[int, int]]:
    """Find CSS declaration boundaries outside strings, comments and functions.

    Ambiguous unfinished CSS is ignored rather than rewritten. This only
    identifies boundaries; it does not implement the CSS grammar or cascade.
    """
    declarations: list[tuple[int, int]] = []
    start = cursor = depth = 0
    quote: str | None = None
    while cursor < len(style):
        char = style[cursor]
        if char == "\\":
            cursor += 2
            continue
        if quote is not None:
            if char == quote:
                quote = None
        elif style.startswith("/*", cursor):
            end = style.find("*/", cursor + 2)
            if end < 0:
                return []
            cursor = end + 2
            continue
        elif char in ('"', "'"):
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            if depth == 0:
                return []
            depth -= 1
        elif char == ";" and depth == 0:
            declarations.append((start, cursor))
            start = cursor + 1
        cursor += 1
    if quote is not None or depth:
        return []
    declarations.append((start, len(style)))
    return declarations


def _style_properties(style: str) -> dict[str, str]:
    properties: dict[str, str] = {}
    important: set[str] = set()
    for start, end in _style_declarations(style):
        declaration = style[start:end]
        name, separator, value = declaration.partition(":")
        if separator:
            name, value = name.strip().lower(), value.strip().lower()
            priority = re.search(r"!\s*important\s*$", value)
            if name in important and priority is None:
                continue
            properties[name] = value[:priority.start()].strip() if priority else value
            if priority:
                important.add(name)
    return properties


def _orange_white(properties: dict[str, str]) -> bool:
    return properties.get("background-color") == "#ff5f05" and properties.get("color") in {
        "white", "#fff", "#ffffff",
    }


def _element_ranges(
    tags: list[HTMLTag], tag: str, *, outermost: bool = False,
) -> Iterator[tuple[HTMLTag, int, int]]:
    """Pair complete tags once; yield opening tags and body offsets, not copies.

    Missing closing tags produce no element. Offsets avoid copying every
    nested table/span body; callers use precomputed marker positions instead.
    Links may be restricted to disjoint outer ranges because nested anchors
    are invalid HTML and their text extraction must not rescan nested bodies.
    """
    opened: list[HTMLTag] = []
    for token in tags:
        if token.name != tag:
            continue
        if token.closing:
            if opened:
                opening = opened.pop()
                if not outermost or not opened:
                    yield opening, opening.end, token.start
        else:
            opened.append(token)


def _has_marker(positions: list[int], start: int, end: int) -> bool:
    return bisect_left(positions, end) > bisect_left(positions, start)


def _fix_th_scope(html: str) -> tuple[str, int]:
    """Add scope='col' to <th> elements missing it."""
    edits = [
        (tag.name_end, tag.name_end, ' scope="col"')
        for tag in iter_html_tags(html)
        if tag.name == "th" and not tag.closing and "scope" not in tag.attributes
    ]
    return _apply_html_edits(html, edits), len(edits)


def _apply_html_edits(html: str, edits: list[tuple[int, int, str]]) -> str:
    """Apply ordered disjoint edits without copying the whole document per tag."""
    parts: list[str] = []
    cursor = 0
    for start, end, replacement in edits:
        parts.extend((html[cursor:start], replacement))
        cursor = end
    parts.append(html[cursor:])
    return "".join(parts)


def _fix_orange_contrast(html: str) -> tuple[str, int]:
    """Fix white text on #ff5f05 orange backgrounds (3.1:1 → 6.77:1)."""
    edits = []
    for tag in iter_html_tags(html):
        if tag.name in {"script", "style"}:
            continue
        attribute = tag.attributes.get("style")
        if attribute is None:
            continue
        original = attribute.value
        if not _orange_white(_style_properties(original)):
            continue
        replacements = []
        for start, end in _style_declarations(original):
            declaration = original[start:end]
            foreground = re.fullmatch(
                r"(\s*)color\s*:\s*(?:white|#fff(?:fff)?)(\s*(?:!\s*important\s*)?)",
                declaration, flags=re.IGNORECASE,
            )
            if foreground:
                replacements.append((start, end, foreground[1] + "color: #000000" + foreground[2]))
        fixed = _apply_html_edits(original, replacements)
        if fixed != original:
            edits.append((attribute.start, attribute.end, fixed))
    return _apply_html_edits(html, edits), len(edits)


# CidiLabs DesignPLUS kl_ → dp- class mapping
# Source: https://support.cidilabs.com/knowledgebase/designplus-css-class-and-id-list
_KL_CLASS_MAP: dict[str, str] = {
    "kl_wrapper": "dp-wrapper",
    "kl_flat_sections": "dp-flat-sections",
    "kl_flat_sections_main": "dp-flat-sections-main",
    "variation_2": "variation-2",
    "kl_mod_text": "dp-header-pre-1",
    "kl_mod_num": "dp-header-pre-2",
    "kl_subtitle": "dp-header-subtitle",
    "kl_ignore": "dp-ignore",
    "kl_locked": "dp-locked",
    "kl_instructions": "dp-action-item",
    "kl_progress_icons": "dp-progress-icons",
    "kl_complete": "dp-complete",
    "kl_colored_bar": "dp-colored-bar",
    "kl_current": "dp-current",
    "kl_pending": "dp-pending",
    "kl_apple": "dp-apple",
    "kl_basic_bar": "dp-basic-bar",
    "kl_basic_color": "dp-basic-color",
    "kl_bookmark": "dp-bookmark",
    "kl_box_left": "dp-box-left",
    "kl_circle_left": "dp-circle-left",
    "kl_emta": "dp-emta",
    "kl_badge": "dp-badge",
    "kl_generic": "dp-generic",
    "kl_ribbons_main": "dp-ribbons-main",
    "kl_rounded_inset": "dp-rounded-inset",
    "kl_square_right": "dp-square-right",
    "kl_colored_headings_box_left": "dp-colored-headings-box-left",
    "kl_basic_color_panel_nav": "dp-basic-color-panel-nav",
}

_KL_ID_TO_CLASS: dict[str, str] = {
    "kl_wrapper_3": "dp-wrapper",
    "kl_banner": "dp-header",
    "kl_banner_left": "dp-header-pre",
    "kl_banner_right": "dp-header-title",
    "kl_description": "dp-header-description",
    "kl_introduction": "dp-content-block",
    "kl_objectives": "dp-content-block",
    "kl_readings": "dp-content-block",
    "kl_lectures": "dp-content-block",
    "kl_activities": "dp-content-block",
    "kl_assignments": "dp-content-block",
}

_KL_ID_PATTERNS: list[tuple[str, str]] = [
    (r"kl_custom_block_\d+", "dp-content-block"),
    (r"kl_content_block_\d+", "dp-content-block"),
    (r"kl_objectives\d+", "dp-content-block"),
    (r"kl_readings\d+", "dp-content-block"),
]


def _fix_legacy_designplus(html: str) -> tuple[str, int]:
    """Migrate kl_ classes/IDs to dp- equivalents."""
    count = 0
    edits = []
    for tag in iter_html_tags(html):
        if not tag.name or tag.closing or tag.name in {"script", "style"}:
            continue
        class_attr = tag.attributes.get("class")
        if (class_attr is not None and class_attr.start == class_attr.end
                and html[class_attr.start - 1:class_attr.start] not in ('"', "'")):
            # A bare/unfinished class attribute has no safe value span to edit.
            continue
        classes = class_attr.value if class_attr is not None else ""
        fixed_classes = re.sub(r"\S+", lambda match: _KL_CLASS_MAP.get(match[0], match[0]), classes)
        if fixed_classes != classes:
            count += 1
        id_attr = tag.attributes.get("id")
        dp_class = _designplus_id_class(id_attr.value) if id_attr else None
        if dp_class and dp_class not in fixed_classes.split():
            fixed_classes = f"{fixed_classes} {dp_class}".strip()
            count += 1
        if fixed_classes == classes:
            continue
        if class_attr is not None:
            if html[class_attr.start - 1:class_attr.start] not in ('"', "'") and len(fixed_classes.split()) > 1:
                fixed_classes = f'"{fixed_classes}"'
            edits.append((class_attr.start, class_attr.end, fixed_classes))
        elif id_attr is not None:
            after_id = id_attr.end + 1 if html[id_attr.end:id_attr.end + 1] in ('"', "'") else id_attr.end
            edits.append((after_id, after_id, f' class="{fixed_classes}"'))
    return _apply_html_edits(html, edits), count


def _designplus_id_class(identifier: str) -> str | None:
    """Resolve known legacy IDs without treating arbitrary content as markup."""
    mapped = _KL_ID_TO_CLASS.get(identifier)
    if mapped:
        return mapped
    return next((cls for pattern, cls in _KL_ID_PATTERNS if re.fullmatch(pattern, identifier)), None)


def _fix_redundant_alt_prefix(html: str) -> tuple[str, int]:
    """Remove 'image of', 'graphic of', etc. prefixes from alt text."""
    edits = []
    for tag in iter_html_tags(html):
        alt = tag.attributes.get("alt")
        if tag.name != "img" or tag.closing or alt is None:
            continue
        prefix = re.match(r"(?:image|graphic|picture|photo|icon)\s+(?:of|showing)\s+", alt.value, re.IGNORECASE)
        if prefix:
            edits.append((alt.start, alt.start + prefix.end(), ""))
    return _apply_html_edits(html, edits), len(edits)


def _extract_violations_from_html(html_content: str) -> list[dict[str, Any]]:
    """Extract accessibility violations from UFIXIT report HTML.

    This parser handles common UFIXIT/UDOIT report formats.
    """
    violations: list[dict[str, Any]] = []

    # Try to find violation patterns in the HTML
    # UFIXIT reports often use tables or lists to display violations

    # Pattern 1: Look for WCAG criterion mentions
    wcag_pattern = r'WCAG\s+(\d+\.\d+\.\d+)'

    # Pattern 2: Look for severity indicators
    severity_pattern = r'(critical|serious|moderate|minor|error|warning)'

    # Pattern 3: Look for common issue types
    issue_patterns = [
        (r'missing\s+alt\s+text', 'missing_alt_text', 'Images missing alternative text'),
        (r'heading\s+structure', 'heading_structure', 'Improper heading hierarchy'),
        (r'color\s+contrast', 'color_contrast', 'Insufficient color contrast'),
        (r'link\s+text', 'link_text', 'Non-descriptive link text'),
        (r'table\s+header', 'table_headers', 'Tables missing proper headers'),
        (r'form\s+label', 'form_labels', 'Form inputs missing labels'),
    ]

    # Extract structured violations from HTML
    # This is a simplified parser - real UFIXIT reports may have different formats
    lines = strip_html_tags(html_content).split('\n')
    current_violation: dict[str, Any] = {}

    for line in lines:
        # Check for WCAG criterion
        wcag_match = re.search(wcag_pattern, line, re.IGNORECASE)
        if wcag_match:
            if current_violation:
                violations.append(current_violation)
            current_violation = {
                "wcag_criterion": wcag_match.group(1),
                "type": "unknown",
                "severity": "moderate"
            }

        # Check for severity
        severity_match = re.search(severity_pattern, line, re.IGNORECASE)
        if severity_match and current_violation:
            current_violation["severity"] = severity_match.group(1).lower()

        # Check for issue types
        for pattern, issue_type, description in issue_patterns:
            if re.search(pattern, line, re.IGNORECASE):
                if current_violation:
                    current_violation["type"] = issue_type
                    current_violation["description"] = description

        # Extract location information
        if 'page' in line.lower() or 'assignment' in line.lower():
            if current_violation and "location" not in current_violation:
                current_violation["location"] = remove_html_tags(line).strip()[:100]

    if current_violation:
        violations.append(current_violation)

    return violations


def _check_content_accessibility(
    html_content: str,
    content_type: str,
    content_id: int | None,
    content_title: str | None
) -> list[dict[str, Any]]:
    """Check HTML content for basic accessibility issues."""
    issues: list[dict[str, Any]] = []

    if not html_content:
        return issues

    tags = list(iter_html_tags(html_content))
    opening_tags = [tag for tag in tags if not tag.closing and tag.name not in {"", "script", "style"}]
    styles = [_style_properties(tag.attributes["style"].value) for tag in opening_tags if "style" in tag.attributes]
    images = [tag for tag in opening_tags if tag.name == "img"]
    image_alts = [
        image.attributes["alt"].value for image in images if "alt" in image.attributes
    ]
    links = list(_element_ranges(tags, "a", outermost=True))

    # Check for images without alt text
    for image in images:
        if "alt" in image.attributes:
            continue
        issues.append({
            "type": "missing_alt_text",
            "wcag_criterion": "1.1.1",
            "wcag_level": "A",
            "severity": "serious",
            "content_type": content_type,
            "content_id": content_id,
            "content_title": content_title,
            "description": "Image missing alt attribute",
            "remediation": "Add descriptive alt text to all images",
            "auto_fixable": False
        })

    # Check for empty headings
    headings = {f"h{level}" for level in range(1, 7)}
    for _, start, end in (
        element for name in sorted(headings) for element in _element_ranges(tags, name, outermost=True)
    ):
        if html_content[start:end].strip():
            continue
        issues.append({
            "type": "empty_heading",
            "wcag_criterion": "2.4.6",
            "wcag_level": "AA",
            "severity": "moderate",
            "content_type": content_type,
            "content_id": content_id,
            "content_title": content_title,
            "description": "Empty heading element found",
            "remediation": "Remove empty headings or add descriptive text",
            "auto_fixable": False
        })

    # Check for tables without headers
    header_positions = [tag.start for tag in opening_tags if tag.name == "th"]
    for _, start, end in _element_ranges(tags, "table"):
        if _has_marker(header_positions, start, end):
            continue
        issues.append({
            "type": "table_without_headers",
            "wcag_criterion": "1.3.1",
            "wcag_level": "A",
            "severity": "serious",
            "content_type": content_type,
            "content_id": content_id,
            "content_title": content_title,
            "description": "Table missing header cells",
            "remediation": "Add <th> elements to define table headers",
            "auto_fixable": False
        })

    # Check for non-descriptive link text
    bad_link_texts = {"click here", "here", "read more", "more", "link", "this"}
    for _, start, end in links:
        if html_content[start:end].lower() in bad_link_texts:
            issues.append({
                "type": "non_descriptive_link",
                "wcag_criterion": "2.4.4",
                "wcag_level": "A",
                "severity": "moderate",
                "content_type": content_type,
                "content_id": content_id,
                "content_title": content_title,
                "description": "Link text is not descriptive",
                "remediation": "Use descriptive link text that explains the destination",
                "auto_fixable": False
            })

    # Check for empty links
    for _, start, end in links:
        if html_content[start:end].strip():
            continue
        issues.append({
            "type": "empty_link",
            "wcag_criterion": "2.4.4",
            "wcag_level": "A",
            "severity": "serious",
            "content_type": content_type,
            "content_id": content_id,
            "content_title": content_title,
            "description": "Link has no text content",
            "remediation": "Add descriptive text or remove the empty link",
            "auto_fixable": False
        })

    # Check for URL-as-link-text
    for _, start, end in links:
        if re.fullmatch(r'\s*https?://[^\s<]+\s*', html_content[start:end], re.IGNORECASE) is None:
            continue
        issues.append({
            "type": "url_as_link_text",
            "wcag_criterion": "2.4.4",
            "wcag_level": "A",
            "severity": "moderate",
            "content_type": content_type,
            "content_id": content_id,
            "content_title": content_title,
            "description": "Raw URL used as link text instead of descriptive text",
            "remediation": "Replace URL with descriptive text explaining the destination",
            "auto_fixable": False
        })

    # Check for <th> without scope attribute (\b prevents matching <thead>)
    for tag in opening_tags:
        if tag.name != "th" or "scope" in tag.attributes:
            continue
        issues.append({
            "type": "th_missing_scope",
            "wcag_criterion": "1.3.1",
            "wcag_level": "A",
            "severity": "serious",
            "content_type": content_type,
            "content_id": content_id,
            "content_title": content_title,
            "description": "Table header <th> missing scope attribute",
            "remediation": "Add scope=\"col\" or scope=\"row\" to all <th> elements",
            "auto_fixable": True
        })

    # Check heading hierarchy (skipped levels)
    heading_levels = [
        int(tag.name[1]) for tag in opening_tags if tag.name in headings
    ]
    for i in range(1, len(heading_levels)):
        if heading_levels[i] > heading_levels[i - 1] + 1:
            issues.append({
                "type": "heading_skip",
                "wcag_criterion": "1.3.1",
                "wcag_level": "A",
                "severity": "moderate",
                "content_type": content_type,
                "content_id": content_id,
                "content_title": content_title,
                "description": f"Heading level skipped: H{heading_levels[i-1]} to H{heading_levels[i]}",
                "remediation": "Use sequential heading levels without skipping (H2 → H3, not H2 → H4)",
                "auto_fixable": False
            })

    # Check for low-contrast text on colored backgrounds
    # Specifically: white/light text on #ff5f05 (Illinois orange) — 3.1:1, fails AA
    for properties in styles:
        if not _orange_white(properties):
            continue
        issues.append({
            "type": "low_contrast",
            "wcag_criterion": "1.4.3",
            "wcag_level": "AA",
            "severity": "serious",
            "content_type": content_type,
            "content_id": content_id,
            "content_title": content_title,
            "description": "White text on #ff5f05 orange background (~3.1:1 contrast, needs 4.5:1)",
            "remediation": "Change text color to #000000 (black) for 6.77:1 contrast on orange",
            "auto_fixable": True
        })

    # Check for legacy DesignPLUS kl_ classes (should be migrated to dp-)
    kl_classes = {
        value for tag in opening_tags for name in ("class", "id")
        if name in tag.attributes for value in tag.attributes[name].value.split()
        if value.startswith("kl_")
    }
    if kl_classes:
        issues.append({
            "type": "legacy_designplus",
            "wcag_criterion": "N/A",
            "wcag_level": "N/A",
            "severity": "minor",
            "content_type": content_type,
            "content_id": content_id,
            "content_title": content_title,
            "description": f"Legacy DesignPLUS kl_ classes found: {', '.join(sorted(kl_classes)[:5])}",
            "remediation": "Migrate kl_ classes to dp- equivalents for new DesignPLUS sidebar",
            "auto_fixable": any(value in _KL_CLASS_MAP or _designplus_id_class(value) for value in kl_classes)
        })

    # Check for images with alt text that looks like a filename
    for alt in image_alts:
        if not re.search(r'\.(jpg|jpeg|png|gif|svg|webp|bmp)', alt, re.IGNORECASE):
            continue
        issues.append({
            "type": "filename_alt_text",
            "wcag_criterion": "1.1.1",
            "wcag_level": "A",
            "severity": "moderate",
            "content_type": content_type,
            "content_id": content_id,
            "content_title": content_title,
            "description": "Image alt text appears to be a filename",
            "remediation": "Replace filename with descriptive alt text",
            "auto_fixable": False
        })

    # Check for images with alt text starting with "image of" or "graphic of"
    for alt in image_alts:
        if not re.match(r'(?:image|graphic|picture|photo|icon)\s+(?:of|showing)\s', alt, re.IGNORECASE):
            continue
        issues.append({
            "type": "redundant_alt_prefix",
            "wcag_criterion": "1.1.1",
            "wcag_level": "A",
            "severity": "minor",
            "content_type": content_type,
            "content_id": content_id,
            "content_title": content_title,
            "description": "Alt text starts with redundant 'image of' / 'graphic of' prefix",
            "remediation": "Remove the prefix — screen readers already announce it as an image",
            "auto_fixable": True
        })

    # Check for very short alt text (likely insufficient)
    for raw_alt in image_alts:
        if not 1 <= len(raw_alt) <= 4:
            continue
        alt = raw_alt.strip()
        # Skip empty alt (decorative) and common short valid alts
        if alt and alt != "&nbsp;" and alt not in ("—", "-", "•", "*", "x", "X"):
            issues.append({
                "type": "short_alt_text",
                "wcag_criterion": "1.1.1",
                "wcag_level": "A",
                "severity": "moderate",
                "content_type": content_type,
                "content_id": content_id,
                "content_title": content_title,
                "description": f"Alt text is very short ({len(alt)} chars): \"{alt}\"",
                "remediation": "Alt text should describe the image content and function",
                "auto_fixable": False
            })

    # Check for very long alt text (should use long description instead)
    for alt in image_alts:
        if len(alt) < 100:
            continue
        issues.append({
            "type": "long_alt_text",
            "wcag_criterion": "1.1.1",
            "wcag_level": "A",
            "severity": "minor",
            "content_type": content_type,
            "content_id": content_id,
            "content_title": content_title,
            "description": f"Alt text is very long ({len(alt)} chars) — consider a long description link",
            "remediation": "Shorten alt text and provide a linked long description for complex images",
            "auto_fixable": False
        })

    # Check for links to documents without file type indicator
    for tag, start, end in links:
        href = tag.attributes.get("href")
        if href is None:
            continue
        extension = re.search(r'\.(pdf|docx?|xlsx?|pptx?|csv)$', href.value, re.IGNORECASE)
        if extension is None:
            continue
        ext = extension.group(1).lower()
        link_text = remove_html_tags(html_content[start:end]).strip()
        ext_names = {"pdf": "PDF", "doc": "Word", "docx": "Word",
                     "xls": "Excel", "xlsx": "Excel",
                     "ppt": "PowerPoint", "pptx": "PowerPoint", "csv": "CSV"}
        label = ext_names.get(ext, ext.upper())
        if label.lower() not in link_text.lower() and ext not in link_text.lower():
            issues.append({
                "type": "doc_link_no_type",
                "wcag_criterion": "2.4.4",
                "wcag_level": "A",
                "severity": "minor",
                "content_type": content_type,
                "content_id": content_id,
                "content_title": content_title,
                "description": f"Link to .{ext} file doesn't indicate file type: \"{link_text[:50]}\"",
                "remediation": f"Add [{label}] to the link text so users know the file type",
                "auto_fixable": False
            })

    # Check for videos/iframes without caption indicators
    for tag in opening_tags:
        if tag.name != "iframe" or "src" not in tag.attributes:
            continue
        src = tag.attributes["src"].value
        # Detect video platforms
        if any(p in src for p in ("youtube.com", "youtu.be", "kaltura.com",
                                   "vimeo.com", "mediaspace")):
            issues.append({
                "type": "video_caption_check",
                "wcag_criterion": "1.2.2",
                "wcag_level": "A",
                "severity": "moderate",
                "content_type": content_type,
                "content_id": content_id,
                "content_title": content_title,
                "description": "Embedded video — verify captions are present and accurate",
                "remediation": "Ensure synchronized captions exist; auto-generated captions should be reviewed",
                "auto_fixable": False
            })

    # Check for underlined non-link text (confuses users)
    for tag in opening_tags:
        style = tag.attributes.get("style")
        if tag.name not in {"span", "p", "strong", "em", "div"} or style is None:
            continue
        if "underline" not in _style_properties(style.value).get("text-decoration", "").split():
            continue
        issues.append({
            "type": "underline_not_link",
            "wcag_criterion": "1.3.1",
            "wcag_level": "A",
            "severity": "minor",
            "content_type": content_type,
            "content_id": content_id,
            "content_title": content_title,
            "description": "Underlined text that is not a link — underlines signal hyperlinks",
            "remediation": "Use bold or italics for emphasis instead of underline",
            "auto_fixable": False
        })

    # Check for small inline font sizes
    for properties in styles:
        size_match = re.fullmatch(r'(\d+)\s*px', properties.get("font-size", ""))
        if size_match is None:
            continue
        digits = size_match.group(1).lstrip("0") or "0"
        # Arbitrarily long caller-supplied integers need no int conversion:
        # any value with two significant digits is already above the cutoff.
        if len(digits) >= 2:
            continue
        size = int(digits)
        if size < 10:
            issues.append({
                "type": "small_font_size",
                "wcag_criterion": "1.4.4",
                "wcag_level": "AA",
                "severity": "moderate",
                "content_type": content_type,
                "content_id": content_id,
                "content_title": content_title,
                "description": f"Inline font-size: {size}px is below readable threshold",
                "remediation": "Use at least 12px or remove inline font-size styling",
                "auto_fixable": False
            })

    # Check for manual bullet characters instead of proper lists
    manual_bullet = re.compile(r'\s*[•●○◦►▸▹–—\-\*]\s+\w')
    if any(tag.name in {"p", "br"} and not tag.closing and manual_bullet.match(html_content, tag.end)
           for tag in opening_tags):
        issues.append({
            "type": "manual_bullets",
            "wcag_criterion": "1.3.1",
            "wcag_level": "A",
            "severity": "minor",
            "content_type": content_type,
            "content_id": content_id,
            "content_title": content_title,
            "description": "Text uses manual bullet characters instead of <ul>/<ol> list elements",
            "remediation": "Convert to proper HTML lists for screen reader navigation",
            "auto_fixable": False
        })

    # Check for color used as sole indicator (inline color styles without bold/italic)
    emphasis = [
        tag.start for tag in opening_tags
        if tag.name in {"strong", "em", "b", "i"} or (
            "style" in tag.attributes and {"font-weight", "font-style"}
            & _style_properties(tag.attributes["style"].value).keys()
        )
    ]
    newlines = [m.start() for m in re.finditer("\n", html_content)]
    for tag, start, end in _element_ranges(tags, "span"):
        style = tag.attributes.get("style")
        if style is None or end - start < 5 or _has_marker(newlines, start, end):
            continue
        properties = _style_properties(style.value)
        color = properties.get("color", "")
        if color and color not in {"inherit", "initial", "unset", "currentcolor"} and not (
            "font-weight" in properties or "font-style" in properties
            or _has_marker(emphasis, start, end)
        ):
            issues.append({
                "type": "color_only_meaning",
                "wcag_criterion": "1.4.1",
                "wcag_level": "A",
                "severity": "minor",
                "content_type": content_type,
                "content_id": content_id,
                "content_title": content_title,
                "description": "Colored text without additional visual indicator (bold/italic)",
                "remediation": "Add bold or italic alongside color so meaning isn't conveyed by color alone",
                "auto_fixable": False
            })

    return issues


def _generate_violation_summary(violations: list[dict[str, Any]]) -> dict[str, Any]:
    """Generate summary statistics from violations."""
    summary: dict[str, Any] = {
        "total_violations": len(violations),
        "by_severity": {},
        "by_type": {},
        "by_wcag_criterion": {},
        "by_content_type": {}
    }

    for violation in violations:
        # Count by severity
        severity = violation.get("severity", "unknown")
        summary["by_severity"][severity] = summary["by_severity"].get(severity, 0) + 1

        # Count by type
        vtype = violation.get("type", "unknown")
        summary["by_type"][vtype] = summary["by_type"].get(vtype, 0) + 1

        # Count by WCAG criterion
        wcag = violation.get("wcag_criterion", "unknown")
        summary["by_wcag_criterion"][wcag] = summary["by_wcag_criterion"].get(wcag, 0) + 1

        # Count by content type
        content_type = violation.get("content_type", "unknown")
        summary["by_content_type"][content_type] = summary["by_content_type"].get(content_type, 0) + 1

    return summary
