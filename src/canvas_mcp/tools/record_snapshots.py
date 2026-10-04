"""Educator-only local capture of Canvas read evidence, with scope preview."""

from __future__ import annotations

import hashlib
import os
import uuid
from pathlib import Path
from typing import Any

from fastmcp import FastMCP
from mcp.types import ToolAnnotations

from ..core.artifact_store import ArtifactStore, canonical_origin, json_bytes
from ..core.cache import get_course_id
from ..core.client import make_canvas_request
from ..core.config import get_config
from ..core.credentials import is_http_request_active
from ..core.path import canvas_path
from ..core.record_snapshots import (
    checkpoint,
    collect_pages,
    new_manifest,
    normalized_scope,
    numeric_id,
    snapshot_summary,
    verify_snapshot,
)
from ..core.write_confirmation import ConfirmationGuard, redeem_confirmation

_GUARD = ConfirmationGuard(nothing_done="No student records were stored.")


async def _capture_context(course_identifier: str | int) -> tuple[str, str, str]:
    course_id = await get_course_id(course_identifier)
    course = await make_canvas_request(
        "get", canvas_path("courses", course_id), _max_response_bytes=1024 * 1024
    )
    if not isinstance(course, dict) or "error" in course:
        raise ValueError("Course unavailable")
    canonical = numeric_id(course.get("id"))
    if (
        str(course_id).isascii()
        and str(course_id).isdecimal()
        and numeric_id(course_id) != canonical
    ):
        raise ValueError("Wrong course response")
    permissions = await make_canvas_request(
        "get",
        canvas_path("courses", canonical, "permissions"),
        params={"permissions[]": ["manage_grades"]},
        _max_response_bytes=1024 * 1024,
    )
    if (
        not isinstance(permissions, dict)
        or permissions.get("manage_grades") is not True
    ):
        raise ValueError("Verified educator grading permission required")
    profile = await make_canvas_request(
        "get",
        "/users/self/profile",
        skip_anonymization=True,
        _max_response_bytes=1024 * 1024,
    )
    if not isinstance(profile, dict) or "error" in profile:
        raise ValueError("Caller unavailable")
    return (
        canonical_origin(get_config().canvas_api_url),
        canonical,
        numeric_id(profile.get("id")),
    )


def register_record_snapshot_tools(mcp: FastMCP) -> None:
    @mcp.tool(
        annotations=ToolAnnotations(
            read_only_hint=False, destructive_hint=False, idempotent_hint=False
        )
    )
    async def capture_record_snapshot(
        course_identifier: str | int,
        save_directory: str = "local_snapshots",
        families: list[str] | None = None,
        assignment_ids: list[int] | None = None,
        include_bodies: bool = False,
        include_comments: bool = False,
        include_identities: bool = False,
        include_attachment_metadata: bool = False,
        page_budget: int = 20,
        resume_directory: str | None = None,
        confirmation_token: str | None = None,
    ) -> dict[str, Any]:
        """Preview then capture a private educator record snapshot using Canvas GETs only.

        Confirm identical arguments after reviewing the local storage scope.
        Each call reads at most page_budget record pages (1..100), plus course,
        permission and caller checks. Resume an interrupted/partial directory
        with the original scope and a fresh preview. Names, content and comments
        are excluded by default. Attachment IDs/size/type may be explicitly
        included for a separate bounded download. Bytes, Inbox and discussions
        are excluded from this capture.
        Numeric IDs remain identifying; summaries contain no student records.
        This is scoped evidence, not a transaction or a restoration operation.
        """
        if is_http_request_active():
            return {
                "error": "Record capture is available only on local stdio. No records were stored."
            }
        if type(page_budget) is not int or not 1 <= page_budget <= 100:
            return {"error": "page_budget must be an integer from 1 through 100."}
        directory: Path | None = None
        try:
            scope = normalized_scope(
                families,
                assignment_ids,
                include_bodies,
                include_comments,
                include_identities,
                include_attachment_metadata,
            )
            origin, course_id, caller_id = await _capture_context(course_identifier)
            destination = Path(save_directory).expanduser().absolute()
            if ".." in destination.parts:
                raise ValueError("Invalid local destination")
            manifest = None
            previous_digest = None
            if resume_directory:
                with ArtifactStore(resume_directory) as previous:
                    previous.lock(shared=True)
                    manifest = verify_snapshot(previous)
                if (
                    manifest["canvas_origin"],
                    manifest["course_id"],
                    manifest["caller_id"],
                    manifest["scope"],
                ) != (origin, course_id, caller_id, scope):
                    return {
                        "error": "Resume origin, course, caller or scope differs. No records were stored."
                    }
                previous_digest = hashlib.sha256(json_bytes(manifest)).hexdigest()
                if manifest["state"] == "complete":
                    return {
                        **snapshot_summary(manifest),
                        "snapshot_directory": str(
                            Path(resume_directory).expanduser().absolute()
                        ),
                        "already_complete": True,
                    }
            preview = {
                "course_id": course_id,
                "canvas_origin": origin,
                "scope": scope,
                "save_directory": str(destination),
                "page_budget": page_budget,
                "resume_directory": str(Path(resume_directory).expanduser().absolute())
                if resume_directory
                else None,
                "previous_checkpoint": previous_digest,
                "notice": "Private local files retain identifying numeric IDs. Reads reflect caller permissions and different observation times. No Canvas writes. Attachments are excluded.",
            }
            fingerprint = hashlib.sha256(
                json_bytes({**preview, "caller_id": caller_id})
            ).hexdigest()
            if not confirmation_token:
                return {
                    "preview": True,
                    "nothing_stored": True,
                    **preview,
                    "confirmation_token": _GUARD.issue(fingerprint),
                }
            rejection = redeem_confirmation(_GUARD, confirmation_token, fingerprint)
            if rejection:
                return {"error": rejection}
            if resume_directory:
                directory = Path(resume_directory).expanduser().absolute()
            else:
                namespace = f"snapshot-{hashlib.sha256(origin.encode()).hexdigest()[:16]}-{course_id}-{uuid.uuid4().hex}"
                with ArtifactStore(destination, create=True) as root:
                    os.mkdir(namespace, mode=0o700, dir_fd=root.fd)
                    os.fsync(root.fd)
                directory = destination / namespace
            with ArtifactStore(directory) as store:
                store.lock()
                if resume_directory:
                    manifest = verify_snapshot(store)
                    if (
                        hashlib.sha256(json_bytes(manifest)).hexdigest()
                        != previous_digest
                    ):
                        return {
                            "error": "Snapshot changed after preview. Obtain a new preview."
                        }
                else:
                    manifest = new_manifest(origin, course_id, caller_id, scope)
                    checkpoint(store, manifest)
                summary = await collect_pages(store, manifest, page_budget)
                return {
                    **summary,
                    "snapshot_directory": str(directory),
                    "next_step": "Verify the local snapshot. For partial/interrupted captures, inspect coverage and resume with the original scope and a fresh preview.",
                }
        except Exception:
            return {
                "snapshot_directory": str(directory) if directory else None,
                "error": "Snapshot capture could not complete. No Canvas writes were made. Check permissions, private storage, and the last checkpoint before resuming. No raw records are included in this error.",
            }
