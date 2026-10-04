"""Page settings MCP tools for Canvas API.

Provides tools for updating page settings (publish/unpublish, front page,
editing roles) separate from content editing.
"""

import datetime
import json
from typing import Any

from fastmcp import FastMCP
from mcp.types import ToolAnnotations

from ..core.cache import get_course_code, get_course_id
from ..core.client import fetch_all_paginated_results, make_canvas_request
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

_REVERT_PAGE_GUARD = ConfirmationGuard(nothing_done="The page was not reverted.")

_DELETE_PAGE_GUARD = ConfirmationGuard(nothing_done="Nothing was deleted.")

# Canvas suppresses update notifications for pages younger than this (issue #234).
_NOTIFY_MIN_PAGE_AGE = datetime.timedelta(minutes=1)

_NOTIFY_IS_NOT_A_SETTING = (
    "notify_of_update is a save-time action, not a stored setting: Canvas never "
    "returns it, so the checkbox in the Canvas UI stays unchecked afterward "
    "regardless. Confirm delivery through the recipients' Canvas notifications, "
    "not through this page."
)


def _notify_of_update_warning(response: dict[str, Any]) -> str:
    """Warn that a requested update notification could not be confirmed.

    Canvas's page representation has no ``notify_of_update`` field -- measured
    against a live instance, a PUT setting it returns 16 keys and none is this
    one -- so the tool must never report it as done (issue #234).

    Two of Canvas's suppression conditions ARE visible in the response, so those
    get a confident "no notification was sent" instead of a vague maybe.
    """
    if not response.get("published", False):
        return unconfirmed_write_warning(
            "the update notification",
            {"Requested": "notify_of_update=True", "Page state": "unpublished"},
            "Canvas does not notify participants about changes to an unpublished "
            "page, so no notification was sent. Publish the page first.",
        )

    created_at = parse_date(response.get("created_at"))
    if created_at is not None:
        # datetime.UTC is 3.11+; this project supports 3.10.
        now = datetime.datetime.now(created_at.tzinfo or datetime.UTC)
        if now - created_at < _NOTIFY_MIN_PAGE_AGE:
            return unconfirmed_write_warning(
                "the update notification",
                {"Requested": "notify_of_update=True", "Page age": "under a minute"},
                "Canvas suppresses update notifications for pages this new, so no "
                "notification was sent.",
            )

    return unconfirmed_write_warning(
        "the update notification",
        {"Requested": "notify_of_update=True",
         "Canvas response": "does not include this field"},
        _NOTIFY_IS_NOT_A_SETTING,
    )


def register_page_tools(mcp: FastMCP) -> None:
    """Register page settings MCP tools."""

    @mcp.tool(annotations=ToolAnnotations(destructive_hint=True, idempotent_hint=False))
    @validate_params
    async def update_page_settings(
        course_identifier: str | int,
        page_url_or_id: str,
        published: bool | None = None,
        front_page: bool | None = None,
        editing_roles: str | None = None,
        notify_of_update: bool | None = None
    ) -> str:
        """Update settings for an existing page (without changing content).

        Args:
            course_identifier: Course code or Canvas ID
            page_url_or_id: Page URL slug or page ID
            published: True to publish, False to unpublish. The course front
                page cannot be unpublished; make another page the front page first
            front_page: True to make this the course front page
            editing_roles: One of: teachers, students, members, public
            notify_of_update: Save-time action, NOT a persisted setting. Asks
                Canvas to notify course participants about THIS edit. Canvas
                never returns the flag, so this tool cannot confirm a
                notification was sent and the Canvas UI checkbox will always
                look unchecked afterward. Has no effect on an unpublished page
                or a page under a minute old.
        """
        course_id = await get_course_id(course_identifier)

        # Build update parameters (only include specified settings)
        wiki_page_params: dict[str, Any] = {}

        if published is not None:
            wiki_page_params["published"] = published

        if front_page is not None:
            wiki_page_params["front_page"] = front_page

        if editing_roles is not None:
            wiki_page_params["editing_roles"] = editing_roles

        if notify_of_update is not None:
            wiki_page_params["notify_of_update"] = notify_of_update

        if not wiki_page_params:
            return "No changes specified. Please provide at least one setting to update (published, front_page, editing_roles, or notify_of_update)."

        # Canvas API expects nested wiki_page object
        update_data = {"wiki_page": wiki_page_params}

        response = await make_canvas_request(
            "put",
            canvas_path('courses', course_id, 'pages', page_url_or_id),
            data=update_data
        )

        if isinstance(response, dict) and "error" in response:
            return f"Error updating page settings: {response['error']}"

        # Format success response
        page_title = response.get("title", "Unknown")
        page_url = response.get("url", page_url_or_id)
        is_published = response.get("published", False)
        is_front_page = response.get("front_page", False)
        roles = response.get("editing_roles", "teachers")
        updated_at = response.get("updated_at")

        course_display = await get_course_code(course_id) or course_identifier

        result = "✅ Page settings updated successfully!\n\n"
        result += f"**{page_title}**\n"
        result += f"  Course: {course_display}\n"
        result += f"  URL: {page_url}\n"
        result += f"  Published: {'Yes' if is_published else 'No'}\n"
        result += f"  Front Page: {'Yes' if is_front_page else 'No'}\n"
        result += f"  Editing Roles: {roles}\n"

        if updated_at:
            result += f"  Updated: {format_date(updated_at)}\n"

        if notify_of_update:
            result += "\n" + _notify_of_update_warning(response)

        return result

    @mcp.tool(annotations=ToolAnnotations(destructive_hint=True, idempotent_hint=False))
    @validate_params
    async def bulk_update_pages(
        course_identifier: str | int,
        page_urls: str,
        published: bool | None = None,
        editing_roles: str | None = None,
        notify_of_update: bool | None = None
    ) -> str:
        """Update settings for multiple pages at once.

        Settings only — this tool cannot rename pages or change page content.
        Use edit_page_content to change a page's body.

        Args:
            course_identifier: Course code or Canvas ID
            page_urls: Comma-separated list of page URL slugs
            published: True to publish all, False to unpublish all
            editing_roles: One of: teachers, students, members, public
            notify_of_update: Save-time action, NOT a persisted setting. Asks
                Canvas to notify course participants about these edits. Canvas
                never returns the flag, so this tool cannot confirm any
                notification was sent. Has no effect on unpublished pages.
        """
        course_id = await get_course_id(course_identifier)

        # Parse page URLs
        urls = [url.strip() for url in page_urls.split(",") if url.strip()]

        if not urls:
            return "No pages specified. Please provide a comma-separated list of page URLs."

        # Build update parameters
        wiki_page_params: dict[str, Any] = {}

        if published is not None:
            wiki_page_params["published"] = published

        if editing_roles is not None:
            wiki_page_params["editing_roles"] = editing_roles

        if notify_of_update is not None:
            wiki_page_params["notify_of_update"] = notify_of_update

        if not wiki_page_params:
            return "No changes specified. Please provide at least one setting to update (published, editing_roles, or notify_of_update)."

        update_data = {"wiki_page": wiki_page_params}

        # Process each page
        success_count = 0
        failed_count = 0
        unpublished_count = 0
        failed_pages = []
        updated_pages = []

        for page_url in urls:
            # Nested wiki_page payload must go as JSON; form encoding turns the
            # inner dict into its Python repr, which Canvas rejects with a 500 (#207)
            response = await make_canvas_request(
                "put",
                canvas_path('courses', course_id, 'pages', page_url),
                data=update_data
            )

            if isinstance(response, dict) and "error" in response:
                failed_count += 1
                failed_pages.append(f"{page_url}: {response['error']}")
            else:
                success_count += 1
                updated_pages.append(response.get("title", page_url))
                if not response.get("published", False):
                    unpublished_count += 1

        # Format result
        course_display = await get_course_code(course_id) or course_identifier

        result = "## Bulk Page Update Results\n\n"
        result += f"**Course:** {course_display}\n"
        result += f"**Total pages:** {len(urls)}\n"
        result += f"**Successful:** {success_count}\n"
        result += f"**Failed:** {failed_count}\n\n"

        if updated_pages:
            result += "### Updated Pages\n"
            for title in updated_pages[:10]:  # Show first 10
                result += f"- ✅ {title}\n"
            if len(updated_pages) > 10:
                result += f"- ... and {len(updated_pages) - 10} more\n"
            result += "\n"

        if failed_pages:
            result += "### Failed Pages\n"
            for error in failed_pages[:5]:  # Show first 5 errors
                result += f"- ❌ {error}\n"
            if len(failed_pages) > 5:
                result += f"- ... and {len(failed_pages) - 5} more errors\n"

        if notify_of_update and success_count:
            facts: dict[str, Any] = {
                "Requested": "notify_of_update=True",
                "Canvas response": "does not include this field",
            }
            if unpublished_count:
                facts["Definitely not notified"] = (
                    f"{unpublished_count} of {success_count} updated page(s) are "
                    "unpublished"
                )
            result += "\n" + unconfirmed_write_warning(
                "the update notifications", facts, _NOTIFY_IS_NOT_A_SETTING
            )

        return result



def _format_page_revision(revision: dict[str, Any]) -> str:
    lines = [f"Revision ID: {revision.get('revision_id')}",
             f"Updated: {revision.get('updated_at')}",
             f"Latest: {revision.get('latest', False)}"]
    if "title" in revision:
        lines.append("Title: " + fence_untrusted_inline(revision["title"], "page revision title"))
    if "body" in revision:
        lines.append("Body:\n" + fence_untrusted(revision["body"] or "", "page revision body"))
    return "\n".join(lines)


def register_educator_page_crud_tools(mcp: FastMCP) -> None:
    """Register educator-only page CRUD tools."""

    # front_page=True displaces the course's CURRENT front page -- Canvas
    # allows only one -- so the whole effect is not additive (#204).
    @mcp.tool(annotations=ToolAnnotations(destructive_hint=True, idempotent_hint=False))
    @validate_params
    async def create_page(course_identifier: str | int,
                         title: str,
                         body: str,
                         published: bool = True,
                         front_page: bool = False,
                         editing_roles: str = "teachers") -> str:
        """Create a page in a Canvas course.

        Pages are published by default, so the page is visible to students on
        creation (subject to any module or access restrictions); pass
        published=False to create a draft.

        Args:
            course_identifier: Course code or Canvas ID
            title: Page title
            body: HTML content for the page
            published: Whether to publish (default: True)
            front_page: Whether to set as front page (default: False)
            editing_roles: Who can edit (default: "teachers")
        """
        # Backstop for issue 239: a fenced read result pasted straight into a
        # write would publish our provenance markers into live course content.
        # Read tools fence titles too, so the title is checked like the body.
        if contains_fence_markers(body) or contains_fence_markers(title):
            return FENCE_LEAK_ERROR

        course_id = await get_course_id(course_identifier)

        data = {
            "wiki_page": {
                "title": title,
                "body": body,
                "published": published,
                "front_page": front_page,
                "editing_roles": editing_roles
            }
        }

        response = await make_canvas_request("post", canvas_path('courses', course_id, 'pages'), data=data)

        if "error" in response:
            return f"Error creating page: {response['error']}"

        page_url = response.get("url", "")
        page_title = response.get("title", title)
        created_at = format_date(response.get("created_at"))
        published_status = "Published" if response.get("published", False) else "Unpublished"

        course_display = await get_course_code(course_id) or course_identifier

        result = f"Successfully created page in Course {course_display}:\n\n"
        result += f"Title: {page_title}\n"
        result += f"URL: {page_url}\n"
        result += f"Status: {published_status}\n"
        result += f"Created: {created_at}\n"

        if front_page:
            result += "Set as front page: Yes\n"

        return result

    @mcp.tool(annotations=ToolAnnotations(destructive_hint=True, idempotent_hint=True))
    @validate_params
    async def edit_page_content(course_identifier: str | int,
                               page_url_or_id: str,
                               new_content: str,
                               title: str | None = None) -> str:
        """Replace the entire HTML body of a page (and optionally its title).

        new_content becomes the whole body: it is not merged or appended, so
        pass the complete page, not a fragment. To change one section, read the
        current body with get_page_content, edit it, and send the full result.
        Publishing state, editing roles, and front-page status
        are unchanged; use update_page_settings for those.

        Args:
            course_identifier: Course code or Canvas ID
            page_url_or_id: Page URL slug or page ID
            new_content: Complete new HTML body for the page (replaces the old body)
            title: Optional new title for the page
        """
        # Backstop for issue 239: refuse to write our own provenance fence
        # markers (added by read tools like get_page_content) into Canvas.
        # Read tools fence titles too, so the title is checked like the body.
        if contains_fence_markers(new_content) or (
            title is not None and contains_fence_markers(title)
        ):
            return FENCE_LEAK_ERROR

        course_id = await get_course_id(course_identifier)

        # Prepare the data for updating the page
        update_data = {
            "wiki_page": {
                "body": new_content
            }
        }

        if title:
            update_data["wiki_page"]["title"] = title

        # Update the page
        response = await make_canvas_request(
            "put",
            canvas_path('courses', course_id, 'pages', page_url_or_id),
            data=update_data
        )

        if "error" in response:
            return f"Error updating page: {response['error']}"

        page_title = response.get("title", "Unknown page")
        updated_at = format_date(response.get("updated_at"))
        course_display = await get_course_code(course_id) or course_identifier

        return f"Successfully updated page '{page_title}' in course {course_display}. Last updated: {updated_at}"

    @mcp.tool(annotations=ToolAnnotations(destructive_hint=True, idempotent_hint=True))
    @validate_params
    async def delete_page(
        course_identifier: str | int,
        page_url_or_id: str,
        require_title_match: str | None = None,
        confirmation_token: str | None = None
    ) -> str:
        """Delete a page. Two-step: preview first, then confirm with the token.

        Permanent — Canvas may retain a recycle-bin copy depending on admin settings.

        Args:
            course_identifier: Course code or Canvas ID
            page_url_or_id: Page URL slug or page ID to delete
            require_title_match: Safety check — only delete if page title matches exactly
            confirmation_token: Token from the preview call; omit to preview
        """
        course_id = await get_course_id(course_identifier)

        page = await make_canvas_request(
            "get", canvas_path('courses', course_id, 'pages', page_url_or_id)
        )
        if "error" in page:
            return f"Error fetching page details: {page['error']}"

        page_title = page.get("title", "Unknown Title")
        page_url = page.get("url", page_url_or_id)
        shown_title = fence_untrusted_inline(page_title, "page title")

        if require_title_match and page_title != require_title_match:
            return (
                f"❌ Title mismatch — deletion aborted.\n\n"
                f"  Expected: {require_title_match}\n"
                f"  Actual:   {shown_title}\n\n"
                f"  Page URL: {page_url}"
            )

        course_display = await get_course_code(course_id) or course_identifier
        fingerprint = _DELETE_PAGE_GUARD.fingerprint(
            "delete_page", str(course_id), str(page_url), page_title
        )
        if not confirmation_token:
            preview = (
                f"Would delete page **{shown_title}** from course {course_display}\n"
                f"  URL slug: {page_url}"
            )
            return preview_with_token(_DELETE_PAGE_GUARD, fingerprint, "delete_page", preview)
        error = redeem_confirmation(_DELETE_PAGE_GUARD, confirmation_token, fingerprint)
        if error:
            return error

        response = await make_canvas_request(
            "delete", canvas_path('courses', course_id, 'pages', page_url_or_id)
        )
        if "error" in response:
            return f"Error deleting page {shown_title}: {response['error']}"

        return (
            f"✅ Page deleted successfully!\n\n"
            f"  **{shown_title}**\n"
            f"  Course: {course_display}\n"
            f"  URL slug: {page_url}\n"
            f"  Status: deleted"
        )


    @mcp.tool(annotations=ToolAnnotations(destructive_hint=False, idempotent_hint=False))
    @validate_params
    async def duplicate_page(
        course_identifier: str | int, page_url_or_id: str,
    ) -> str:
        """Duplicate a course page using Canvas's native duplication endpoint.

        Canvas controls the duplicate title and publication state. Review the
        returned state before adding it to modules or publishing it.
        """
        course_id = await get_course_id(course_identifier)
        response = await make_canvas_request(
            "post", canvas_path("courses", course_id, "pages", page_url_or_id, "duplicate")
        )
        if not isinstance(response, dict):
            return "Error: Canvas returned an invalid page response."
        if "error" in response:
            return f"Error duplicating page: {response['error']}"
        return ("Page duplicated.\nTitle: "
                + fence_untrusted_inline(response.get("title") or "Untitled", "page title")
                + f"\nPage ID: {response.get('page_id')}"
                + "\nURL: " + fence_untrusted_inline(response.get("url") or "", "page URL")
                + f"\nPublished: {response.get('published', False)}")

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    @validate_params
    async def list_page_revisions(
        course_identifier: str | int, page_url_or_id: str,
    ) -> str:
        """List page revision metadata. Canvas requires page edit permission.

        Returns revision IDs/timestamps; omits historical editor identities.
        Use get_page_revision to read the content of one revision.
        """
        course_id = await get_course_id(course_identifier)
        revisions = await fetch_all_paginated_results(
            canvas_path("courses", course_id, "pages", page_url_or_id, "revisions")
        )
        if isinstance(revisions, dict) and "error" in revisions:
            return f"Error listing page revisions: {revisions['error']}"
        if not isinstance(revisions, list) or any(not isinstance(r, dict) for r in revisions):
            return "Error: Canvas returned an invalid revision list."
        return "\n\n".join(_format_page_revision(r) for r in revisions) or "No revisions found."

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    @validate_params
    async def get_page_revision(
        course_identifier: str | int, page_url_or_id: str,
        revision_id: str | int = "latest",
    ) -> str:
        """Read a page revision's title/body; requires Canvas edit permission.

        revision_id accepts a positive revision ID or latest.
        Historical editor identities are omitted.
        """
        if str(revision_id) != "latest" and (not str(revision_id).isdigit() or int(revision_id) <= 0):
            return "Error: revision_id must be a positive integer or latest."
        course_id = await get_course_id(course_identifier)
        response = await make_canvas_request(
            "get", canvas_path("courses", course_id, "pages", page_url_or_id, "revisions", revision_id)
        )
        if not isinstance(response, dict):
            return "Error: Canvas returned an invalid page response."
        if "error" in response:
            return f"Error fetching page revision: {response['error']}"
        return _format_page_revision(response)

    @mcp.tool(annotations=ToolAnnotations(destructive_hint=True, idempotent_hint=False))
    @validate_params
    async def revert_page_revision(
        course_identifier: str | int, page_url_or_id: str, revision_id: int,
        confirmation_token: str | None = None,
    ) -> str:
        """Preview then confirm replacing a page with one historical revision.

        Show the preview to the user before confirming. The single-use token
        stops matching if the current page or target revision changes.
        """
        if revision_id <= 0:
            return "Error: revision_id must be positive."
        course_id = await get_course_id(course_identifier)
        path = canvas_path("courses", course_id, "pages", page_url_or_id)
        page = await make_canvas_request("get", path)
        if not isinstance(page, dict):
            return "Error: Canvas returned an invalid current page response."
        if "error" in page:
            return f"Error fetching current page: {page['error']}"
        revision_path = canvas_path("courses", course_id, "pages", page_url_or_id, "revisions", revision_id)
        revision = await make_canvas_request("get", revision_path)
        if not isinstance(revision, dict):
            return "Error: Canvas returned an invalid revision response."
        if "error" in revision:
            return f"Error fetching target revision: {revision['error']}"
        if any(not isinstance(revision.get(k), str) for k in ("body", "title")) or any(not isinstance(page.get(k), str) for k in ("body", "title")):
            return "Error: Canvas did not return the target revision content. Nothing was reverted."
        fingerprint = _REVERT_PAGE_GUARD.fingerprint(
            "revert_page_revision", str(course_id), str(page_url_or_id), str(revision_id),
            json.dumps({k: page.get(k) for k in ("page_id", "url", "title", "body", "updated_at", "published")}, sort_keys=True),
            json.dumps({k: revision.get(k) for k in ("revision_id", "title", "body", "updated_at")}, sort_keys=True),
        )
        if not confirmation_token:
            preview = ("Would replace current page "
                       + fence_untrusted_inline(page.get("title") or "Untitled", "page title")
                       + " with revision:\n" + _format_page_revision(revision))
            return preview_with_token(_REVERT_PAGE_GUARD, fingerprint, "revert_page_revision", preview, action="revert")
        error = redeem_confirmation(_REVERT_PAGE_GUARD, confirmation_token, fingerprint)
        if error:
            return error
        response = await make_canvas_request("post", revision_path)
        if not isinstance(response, dict):
            return "Error: Canvas returned an invalid page response."
        if "error" in response:
            return f"Error reverting page: {response['error']}"
        stable_id = page.get("page_id")
        verify_path = canvas_path("courses", course_id, "pages", f"page_id:{stable_id}") if stable_id else path
        verified = await make_canvas_request("get", verify_path)
        if not isinstance(verified, dict) or "error" in verified or any(verified.get(k) != revision[k] for k in ("body", "title")):
            return unconfirmed_write_warning("the page revision replacement", {"Revision": revision_id}, "Canvas did not verify the requested title and body. Read the page before retrying.")
        return f"Page reverted to revision {revision_id}."

    @mcp.tool(annotations=ToolAnnotations(destructive_hint=True, idempotent_hint=True))
    @validate_params
    async def schedule_page_publication(
        course_identifier: str | int, page_url_or_id: str, publish_at: str,
    ) -> str:
        """Schedule future publication, unpublishing an already published page.

        Requires the account's Scheduled Page Publication feature. Canvas
        ignores publish_at when disabled. This tool verifies the stored date
        and unpublished state, but cannot promise future notification/delivery.
        Use an ISO 8601 timestamp with an explicit timezone.
        """
        try:
            requested = datetime.datetime.fromisoformat(publish_at.replace("Z", "+00:00"))
        except ValueError:
            return "Error: publish_at must be an ISO 8601 timestamp with a timezone."
        if requested.tzinfo is None or requested <= datetime.datetime.now(datetime.UTC):
            return "Error: publish_at must be a future timestamp with a timezone."
        course_id = await get_course_id(course_identifier)
        path = canvas_path("courses", course_id, "pages", page_url_or_id)
        page = await make_canvas_request("get", path)
        if not isinstance(page, dict):
            return "Error: Canvas returned an invalid current page response."
        if "error" in page:
            return f"Error fetching page: {page['error']}"
        if page.get("front_page"):
            return "Error: the course front page cannot be unpublished. Select another front page before scheduling."
        response = await make_canvas_request("put", path, data={"wiki_page": {"publish_at": requested.isoformat()}})
        if not isinstance(response, dict):
            return "Error: Canvas returned an invalid page response."
        if "error" in response:
            return f"Error scheduling page: {response['error']}"
        verified = await make_canvas_request("get", path)
        actual = parse_date(verified.get("publish_at")) if isinstance(verified, dict) and isinstance(verified.get("publish_at"), str) and "error" not in verified else None
        if actual != requested or not isinstance(verified, dict) or verified.get("published") is not False:
            return unconfirmed_write_warning("scheduled page publication", {"Requested": requested.isoformat()}, 'The account must enable "Scheduled Page Publication". Canvas did not confirm the date and unpublished state; read the page settings before retrying.')
        return f"Page publication scheduled for {requested.isoformat()}. Scheduled Page Publication must remain enabled."
