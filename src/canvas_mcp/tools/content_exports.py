"""Creator-safe local backups using Canvas course content exports."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastmcp import FastMCP
from mcp.types import ToolAnnotations

from ..core.cache import get_course_code, get_course_id
from ..core.client import (
    canvas_authenticated_client,
    fetch_all_paginated_results,
    make_canvas_request,
)
from ..core.config import get_config
from ..core.credentials import is_http_request_active
from ..core.file_validation import format_file_size, sanitize_filename
from ..core.path import canvas_path
from ..core.untrusted_content import fence_untrusted_inline
from ..core.validation import coerce_canvas_id, validate_params
from ..core.write_confirmation import unconfirmed_write_warning

_EXPORT_TYPE = "common_cartridge"
_ACTIVE_STATES = {"created", "exporting", "waiting_for_external_tool"}
_INITIAL_POLL_RETRY_SECONDS = 5
_MAX_POLL_RETRY_SECONDS = 60
_RECOMMENDED_POLL_WINDOW_SECONDS = 15 * 60


def _same_canvas_origin(url: object) -> bool:
    if not isinstance(url, str):
        return False
    try:
        target = urlsplit(url)
        canvas = urlsplit(get_config().canvas_api_url)
        target_port = target.port or (443 if target.scheme == "https" else 80)
        canvas_port = canvas.port or (443 if canvas.scheme == "https" else 80)
    except ValueError:
        return False
    return (
        target.scheme.lower() == canvas.scheme.lower()
        and (target.hostname or "").lower() == (canvas.hostname or "").lower()
        and target_port == canvas_port
    )


def _poll_retry_seconds(poll_attempt: int) -> int:
    exponent = min(poll_attempt, 4)
    return min(
        _INITIAL_POLL_RETRY_SECONDS * (1 << exponent),
        _MAX_POLL_RETRY_SECONDS,
    )


def _export_status(
    export: dict[str, Any], course_id: str, poll_attempt: int = 0
) -> dict[str, Any]:
    export_id = coerce_canvas_id(export.get("id", ""))
    state = str(export.get("workflow_state") or "unknown")
    attachment = export.get("attachment")
    attachment_info: dict[str, Any] | None = None
    if isinstance(attachment, dict):
        raw_name = attachment.get("display_name") or attachment.get("filename")
        attachment_info = {
            "filename": (
                fence_untrusted_inline(raw_name, "course export filename")
                if raw_name
                else None
            ),
            "size": attachment.get("size"),
            "content_type": attachment.get("content-type"),
        }

    result: dict[str, Any] = {
        "course_id": course_id,
        "export_id": export_id,
        "export_type": export.get("export_type"),
        "workflow_state": state,
        "created_at": export.get("created_at"),
        "terminal": state not in _ACTIVE_STATES,
        "poll_again": state in _ACTIVE_STATES,
        "download_available": bool(
            state == "exported"
            and isinstance(attachment, dict)
            and attachment.get("url")
        ),
        "attachment": attachment_info,
    }
    if result["poll_again"]:
        result["poll_attempt"] = poll_attempt
        result["retry_after_seconds"] = _poll_retry_seconds(poll_attempt)
        result["recommended_poll_window_seconds"] = (
            _RECOMMENDED_POLL_WINDOW_SECONDS
        )
    if state == "waiting_for_external_tool":
        result["status_note"] = (
            "Canvas is waiting for an external content service. This is a "
            "transient export state, commonly caused by New Quizzes, and can "
            "last several minutes. Keep polling; do not treat it as a failure."
        )
    if export_id is not None and (
        result["poll_again"] or result["download_available"]
    ):
        result["next_action"] = {
            "tool": (
                "get_course_export_status"
                if result["poll_again"]
                else "download_course_export"
            ),
            "arguments": {
                "course_identifier": course_id,
                "export_id": export_id,
                **(
                    {"poll_attempt": poll_attempt + 1}
                    if result["poll_again"]
                    else {}
                ),
            },
        }
    if state == "failed":
        result["error"] = "Canvas reports that the course export failed."
    elif state == "exported" and not result["download_available"]:
        result["error"] = (
            "Canvas reports that the export completed, but its download is no "
            "longer available. Create a new export."
        )
    return result


def _unconfirmed_export_error(course_id: str, detail: object) -> dict[str, Any]:
    return {
        "error": unconfirmed_write_warning(
            "whether Canvas created the course export record",
            {"Course ID": course_id, "Detail": detail},
            (
                "Call list_course_exports before retrying; a timeout can occur "
                "after Canvas queued the export."
            ),
        ),
        "export_start_unconfirmed": True,
    }


def register_content_export_tools(mcp: FastMCP) -> None:
    """Register course-content export and local download tools."""

    @mcp.tool(
        annotations=ToolAnnotations(destructive_hint=False, idempotent_hint=False)
    )
    @validate_params
    async def create_course_export(course_identifier: str | int) -> dict[str, Any]:
        """Start a full Common Cartridge course-content export.

        The export contains course content, not enrollments, submissions,
        student interactions, or grades. Canvas builds it asynchronously; use
        get_course_export_status until poll_again is false. New Quiz content
        can leave an export waiting_for_external_tool for several minutes.
        """
        course_id = await get_course_id(course_identifier)
        try:
            response = await make_canvas_request(
                "post",
                canvas_path("courses", course_id, "content_exports"),
                data={
                    "export_type": _EXPORT_TYPE,
                    "skip_notifications": "true",
                },
                use_form_data=True,
            )
        except Exception as exc:
            return _unconfirmed_export_error(str(course_id), exc)
        if not isinstance(response, dict):
            return _unconfirmed_export_error(
                str(course_id), "Canvas returned an invalid response."
            )
        if "error" in response:
            return _unconfirmed_export_error(str(course_id), response.get("error"))
        export_id = coerce_canvas_id(response.get("id", ""))
        if export_id is None:
            return _unconfirmed_export_error(
                str(course_id), "Canvas did not return a numeric export ID."
            )
        return {
            "export_created": True,
            **_export_status(response, str(course_id)),
            "next_action": {
                "tool": "get_course_export_status",
                "arguments": {
                    "course_identifier": str(course_id),
                    "export_id": export_id,
                    "poll_attempt": 1,
                },
            },
        }

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    @validate_params
    async def list_course_exports(
        course_identifier: str | int,
    ) -> dict[str, Any]:
        """List course-content exports newest first without signed URLs."""
        course_id = await get_course_id(course_identifier)
        response = await fetch_all_paginated_results(
            canvas_path("courses", course_id, "content_exports"),
            {"per_page": 100},
        )
        if isinstance(response, dict) and "error" in response:
            return {"error": f"Could not list course exports: {response['error']}"}
        if not isinstance(response, list):
            return {"error": "Canvas returned an invalid course export list."}
        exports = [
            _export_status(export, str(course_id))
            for export in response
            if isinstance(export, dict)
        ]
        return {
            "course_id": str(course_id),
            "count": len(exports),
            "exports": exports,
        }

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    @validate_params
    async def get_course_export_status(
        course_identifier: str | int,
        export_id: str | int,
        poll_attempt: int = 0,
    ) -> dict[str, Any]:
        """Read one course-content export status without waiting.

        Pass the next_action arguments unchanged so poll_attempt increases and
        retry_after_seconds backs off from 5 to at most 60 seconds. Continue
        for up to recommended_poll_window_seconds. The
        waiting_for_external_tool state is transient and can last several
        minutes, especially when an export contains New Quizzes. Reaching the
        recommended window means the export is still processing, not failed.
        """
        course_id = await get_course_id(course_identifier)
        canonical_export_id = coerce_canvas_id(export_id)
        if canonical_export_id is None:
            return {"error": "export_id must be a positive numeric Canvas ID."}
        if isinstance(poll_attempt, bool) or poll_attempt < 0:
            return {"error": "poll_attempt must be a non-negative integer."}
        response = await make_canvas_request(
            "get",
            canvas_path(
                "courses", course_id, "content_exports", canonical_export_id
            ),
        )
        if not isinstance(response, dict):
            return {"error": "Canvas returned an invalid course export response."}
        if "error" in response:
            return {"error": f"Could not read the course export: {response['error']}"}
        return _export_status(response, str(course_id), poll_attempt)

    @mcp.tool(
        annotations=ToolAnnotations(destructive_hint=False, idempotent_hint=True)
    )
    @validate_params
    async def download_course_export(
        course_identifier: str | int,
        export_id: str | int,
        save_directory: str,
    ) -> dict[str, Any]:
        """Download a completed course export to an existing local directory.

        This tool is available only on a local stdio server. It refuses to
        overwrite an existing file and returns a SHA-256 digest for the saved
        Common Cartridge package.
        """
        if is_http_request_active():
            return {
                "error": (
                    "download_course_export writes to the MCP server's filesystem "
                    "and is available only on a local stdio server."
                )
            }
        course_id = await get_course_id(course_identifier)
        canonical_export_id = coerce_canvas_id(export_id)
        if canonical_export_id is None:
            return {"error": "export_id must be a positive numeric Canvas ID."}
        response = await make_canvas_request(
            "get",
            canvas_path(
                "courses", course_id, "content_exports", canonical_export_id
            ),
        )
        if not isinstance(response, dict):
            return {"error": "Canvas returned an invalid course export response."}
        if "error" in response:
            return {"error": f"Could not read the course export: {response['error']}"}
        status = _export_status(response, str(course_id))
        if status["workflow_state"] != "exported":
            return {
                "error": (
                    "The course export is not ready to download; its Canvas "
                    f"state is {status['workflow_state']}."
                ),
                **status,
            }
        attachment = response.get("attachment")
        if not isinstance(attachment, dict) or not attachment.get("url"):
            return {
                "error": (
                    "Canvas no longer provides a download for this export. "
                    "Create a new export."
                ),
                **status,
            }
        if not _same_canvas_origin(attachment.get("url")):
            return {
                "error": (
                    "Canvas returned an export download URL on an unexpected "
                    "origin; refusing to send the Canvas token to it."
                )
            }

        destination = Path(save_directory).expanduser().resolve()
        if not destination.is_dir():
            return {"error": f"Save directory does not exist: {save_directory}"}
        course_display = await get_course_code(course_id) or str(course_id)
        safe_course_display = sanitize_filename(
            str(course_display).replace("/", "_").replace("\\", "_")
        )
        filename = sanitize_filename(
            f"{safe_course_display}-canvas-export-{canonical_export_id}.imscc"
        )
        save_path = (destination / filename).resolve()
        if not save_path.is_relative_to(destination):
            return {"error": "Invalid export filename outside the save directory."}

        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(save_path, flags, 0o600)
        except FileExistsError:
            return {
                "error": (
                    f"{save_path} already exists. Refusing to overwrite the "
                    "existing backup."
                )
            }
        except OSError as exc:
            return {"error": f"Could not create the backup file: {exc}"}

        digest = hashlib.sha256()
        total_bytes = 0
        try:
            with os.fdopen(fd, "wb") as output:
                async with canvas_authenticated_client() as client:
                    async with client.stream(
                        "GET", str(attachment["url"]), follow_redirects=True
                    ) as download:
                        download.raise_for_status()
                        async for chunk in download.aiter_bytes(chunk_size=8192):
                            output.write(chunk)
                            digest.update(chunk)
                            total_bytes += len(chunk)
                output.flush()
                os.fsync(output.fileno())
        except Exception as exc:
            try:
                os.unlink(save_path)
            except OSError:
                pass
            return {"error": f"Could not download the course export: {exc}"}

        expected_size = attachment.get("size")
        if isinstance(expected_size, int) and expected_size != total_bytes:
            try:
                os.unlink(save_path)
            except OSError:
                pass
            return {
                "error": (
                    "Downloaded size did not match Canvas metadata; the incomplete "
                    "backup was removed."
                ),
                "expected_bytes": expected_size,
                "downloaded_bytes": total_bytes,
            }

        return {
            "downloaded": True,
            "course_id": str(course_id),
            "export_id": canonical_export_id,
            "path": str(save_path),
            "size_bytes": total_bytes,
            "size": format_file_size(total_bytes),
            "sha256": digest.hexdigest(),
        }
