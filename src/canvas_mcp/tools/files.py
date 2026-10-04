"""File-related MCP tools for Canvas API.

Provides tools for uploading, downloading, reading, and listing files in Canvas courses.
Uploaded files can be used with other tools like add_module_item (for adding
files to modules) and send_conversation (for attaching files to messages).

The Canvas file upload process uses a 3-step protocol:
1. Request upload URL from Canvas API
2. Upload file to external storage (S3/Instructure)
3. Confirm upload and get final file object

This module handles all three steps transparently.
"""

import base64
import json
import os
import tempfile
from typing import Any

from fastmcp import FastMCP
from mcp.types import ToolAnnotations

from ..core.cache import get_course_code, get_course_id
from ..core.client import (
    canvas_authenticated_client,
    fetch_all_paginated_results,
    make_canvas_request,
    upload_file_to_storage,
)
from ..core.config import get_config
from ..core.credentials import is_http_request_active
from ..core.dates import parse_date
from ..core.file_validation import (
    FileValidationResult,
    format_file_size,
    sanitize_filename,
    validate_file_for_upload,
)
from ..core.path import canvas_path
from ..core.untrusted_content import (
    FENCE_LEAK_ERROR,
    contains_fence_markers,
    fence_untrusted_inline,
)
from ..core.validation import validate_params
from ..core.write_confirmation import (
    ConfirmationGuard,
    preview_with_token,
    redeem_confirmation,
    unconfirmed_write_warning,
)
from ..core.write_outcome import RequestFailure, WriteOutcome

_DELETE_FILE_GUARD = ConfirmationGuard(nothing_done="Nothing was deleted.")
_DELETE_FOLDER_GUARD = ConfirmationGuard(nothing_done="The folder was not deleted.")


async def _file_module_references(
    course_id: str | int, file_id: str | int
) -> list[dict[str, Any]] | dict[str, str]:
    """Return every module item that directly references a course file."""
    modules = await fetch_all_paginated_results(
        canvas_path("courses", course_id, "modules"),
        {"per_page": 100, "include[]": ["items"]},
    )
    if isinstance(modules, dict) and "error" in modules:
        return {"error": str(modules["error"])}

    references: list[dict[str, Any]] = []
    for module in modules:
        items = module.get("items") or []
        if len(items) < (module.get("items_count") or 0):
            items = await fetch_all_paginated_results(
                canvas_path("courses", course_id, "modules", module.get("id"), "items"),
                {"per_page": 100},
            )
            if isinstance(items, dict) and "error" in items:
                return {"error": str(items["error"])}
        for item in items:
            if item.get("type") == "File" and str(item.get("content_id")) == str(
                file_id
            ):
                references.append(
                    {
                        "module_id": module.get("id"),
                        "module_name": module.get("name", "Unknown"),
                        "item_id": item.get("id"),
                        "item_title": item.get("title", "Unknown"),
                    }
                )
    references.sort(key=lambda ref: (str(ref["module_id"]), str(ref["item_id"])))
    return references


def _folder_fields(
    name: str | None,
    lock_at: str | None,
    unlock_at: str | None,
    locked: bool | None,
    hidden: bool | None,
    position: int | None,
    clear_lock_at: bool = False,
    clear_unlock_at: bool = False,
) -> dict[str, Any] | str:
    if name is not None and contains_fence_markers(name):
        return FENCE_LEAK_ERROR
    if name is not None and (not name.strip() or len(name) > 255):
        return "Invalid name: folder names must contain 1 to 255 characters."
    if position is not None and position < 0:
        return "Invalid position: must be nonnegative."
    fields = {
        key: value
        for key, value in (("name", name), ("locked", locked), ("hidden", hidden), ("position", position))
        if value is not None
    }
    for key, value, clear in (
        ("lock_at", lock_at, clear_lock_at),
        ("unlock_at", unlock_at, clear_unlock_at),
    ):
        if value is not None and clear:
            return f"Invalid configuration: {key} and clear_{key} cannot both be provided."
        if clear:
            fields[key] = ""
        elif value is not None:
            parsed = parse_date(value)
            if parsed is None:
                return f"Invalid date format for {key}. Use ISO 8601 format."
            fields[key] = parsed.isoformat()
    return fields


def _positive_folder_id(value: Any) -> bool:
    return (
        isinstance(value, (str, int))
        and not isinstance(value, bool)
        and str(value).isascii()
        and str(value).isdigit()
        and int(value) > 0
    )


async def _folder_course_id(course_identifier: str | int) -> str | dict[str, str]:
    course_id = await get_course_id(course_identifier)
    if str(course_id).startswith("sis_course_id:"):
        course = await make_canvas_request("get", canvas_path("courses", course_id))
        if not isinstance(course, dict):
            return {"error": "Cannot verify course identity: invalid Canvas response."}
        if "error" in course:
            return {"error": str(course["error"])}
        if not _positive_folder_id(course.get("id")):
            return {"error": "Cannot verify course identity."}
        return str(course["id"])
    if not _positive_folder_id(course_id):
        return {"error": "Cannot verify course identity."}
    return str(course_id)


def _folder_ownership_error(folder: Any, course_id: str) -> str | None:
    if not isinstance(folder, dict) or not _positive_folder_id(folder.get("id")):
        return "Canvas returned an invalid folder response."
    if folder.get("context_type") != "Course" or str(folder.get("context_id")) != course_id:
        return "Folder does not belong to the requested course."
    if folder.get("for_submissions"):
        return "Submission folders are outside course-content folder tools."
    return None


async def _get_course_folder(course_id: str, folder_id: str | int) -> dict[str, Any]:
    if str(folder_id) != "root" and not _positive_folder_id(folder_id):
        return {"error": "Folder ID must be a positive integer or 'root'."}
    folder = await make_canvas_request(
        "get", canvas_path("courses", course_id, "folders", folder_id)
    )
    if isinstance(folder, dict) and "error" in folder:
        return folder
    error = _folder_ownership_error(folder, course_id)
    if error:
        return {"error": error}
    if not isinstance(folder, dict):
        return {"error": "Canvas returned an invalid folder response."}
    if not folder.get("id") or (str(folder_id) != "root" and str(folder.get("id")) != str(folder_id)):
        return {"error": "Canvas returned a different folder ID."}
    return folder


def _format_course_folder(folder: dict[str, Any]) -> str:
    result = f"  Folder: {fence_untrusted_inline(folder.get('name', 'Unknown'), 'folder name')}\n"
    if folder.get("full_name") is not None:
        result += f"  Path: {fence_untrusted_inline(folder['full_name'], 'folder path')}\n"
    for key in ("id", "parent_folder_id", "position", "locked", "hidden", "lock_at", "unlock_at", "files_count", "folders_count"):
        if key in folder:
            result += f"  {key}: {folder[key]}\n"
    return result


def register_shared_file_tools(mcp: FastMCP) -> None:
    """Register file tools accessible to both students and educators."""

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    @validate_params
    async def list_course_folders(course_identifier: str | int) -> str:
        """List all course-content folders as a flat list, including subfolders.

        Args:
            course_identifier: Course code, Canvas ID, or explicit SIS ID
        """
        course_id = await _folder_course_id(course_identifier)
        if isinstance(course_id, dict):
            return f"Error resolving course: {course_id['error']}"
        folders = await fetch_all_paginated_results(
            canvas_path("courses", course_id, "folders"), {"per_page": 100}
        )
        if isinstance(folders, dict) and "error" in folders:
            return f"Error listing folders: {folders['error']}"
        if not isinstance(folders, list):
            return "Error listing folders: Canvas returned an invalid folder list."
        for folder in folders:
            if error := _folder_ownership_error(folder, course_id):
                return f"Error listing folders: {error}"
        if not folders:
            return "No folders found."
        course_display = await get_course_code(course_id) or course_identifier
        return (
            f"Folders in {course_display}:\n\n"
            + "\n".join(_format_course_folder(folder) for folder in folders)
            + f"\nTotal: {len(folders)} folder(s)"
        )

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    @validate_params
    async def get_course_folder(
        course_identifier: str | int, folder_id: str | int = "root"
    ) -> str:
        """Read a course-content folder, using 'root' for the course root.

        Args:
            course_identifier: Course code, Canvas ID, or explicit SIS ID
            folder_id: Canvas folder ID or 'root'
        """
        course_id = await _folder_course_id(course_identifier)
        if isinstance(course_id, dict):
            return f"Error resolving course: {course_id['error']}"
        folder = await _get_course_folder(course_id, folder_id)
        if "error" in folder:
            return f"Error fetching folder: {folder['error']}"
        return _format_course_folder(folder)

    # Writes a new file on the server's filesystem, so it is not read-only. It
    # opens with O_EXCL and never replaces an existing path (additive), and a
    # repeat fails without writing (idempotent).
    @mcp.tool(annotations=ToolAnnotations(
        read_only_hint=False, destructive_hint=False, idempotent_hint=True
    ))
    @validate_params
    async def download_course_file(
        course_identifier: str | int,
        file_id: str | int,
        save_directory: str | None = None,
    ) -> str:
        """Download a file from a Canvas course to the local filesystem.

        Only available on a local (stdio) server. Use read_course_file to get
        file content back in the response instead.

        Use list_course_files or list_module_items to find file IDs.

        Args:
            course_identifier: Course code or Canvas ID
            file_id: Canvas file ID
            save_directory: Local directory to save to (default: system temp dir, must exist)
        """
        # This tool writes to the *server's* filesystem. On a local stdio server
        # that is the caller's own machine; on a shared HTTP one it is somebody
        # else's host, and the caller picks both the destination directory and
        # (via the Canvas file they choose) the filename and bytes — an arbitrary
        # write primitive against the service account. There is also no reason a
        # remote caller would want it: they cannot read what lands there.
        if is_http_request_active():
            return (
                "Error: 'download_course_file' writes to the server's filesystem and is "
                "only available on a local (stdio) server. On this hosted server, use "
                "read_course_file instead, which returns the content in the response."
            )

        course_id = await get_course_id(course_identifier)

        # Get file metadata from Canvas API
        file_info = await make_canvas_request(
            "get",
            canvas_path('courses', course_id, 'files', file_id)
        )

        if isinstance(file_info, dict) and "error" in file_info:
            return f"Error getting file info: {file_info['error']}"

        raw_filename = file_info.get("display_name") or file_info.get("filename", f"file_{file_id}")
        filename = sanitize_filename(raw_filename)
        download_url = file_info.get("url")
        content_type = file_info.get("content-type", "unknown")

        if not download_url:
            return "Error: No download URL available for this file. Check permissions."

        # Determine save path with symlink resolution
        from pathlib import Path
        save_dir = Path(save_directory or tempfile.gettempdir()).resolve()
        if not save_dir.is_dir():
            return f"Error: Directory does not exist: {save_directory}"

        save_path = (save_dir / filename).resolve()
        if not save_path.is_relative_to(save_dir):
            return "Error: Invalid filename - path outside allowed directory"

        # Create the destination exclusively. Canvas controls the filename, so a
        # plain 'wb' open lets a course file named e.g. ".zshrc" silently truncate
        # a real file in whatever directory was chosen. O_EXCL refuses an existing
        # path (including a pre-planted symlink) and O_NOFOLLOW refuses to follow
        # one, closing the swap race between the containment check and the write.
        # O_NOFOLLOW is POSIX-only; on Windows the attribute does not exist at
        # all, so naming it directly would raise AttributeError before os.open
        # runs and break every local download there. O_EXCL alone still refuses
        # an existing path, including a pre-planted symlink, which is the bulk
        # of the protection.
        open_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(save_path, open_flags, 0o600)
        except FileExistsError:
            return (
                f"Error: '{save_path}' already exists. Refusing to overwrite it — "
                f"remove it first or pass a different save_directory."
            )
        except OSError as e:
            return f"Error creating destination file: {e}"

        # Wrap the descriptor immediately so it is closed even if the network call
        # below fails before the first write, and download by streaming to handle
        # large files efficiently.
        try:
            total_bytes = 0
            with os.fdopen(fd, 'wb') as f:
                async with canvas_authenticated_client() as client:
                    async with client.stream(
                        "GET", download_url, follow_redirects=True
                    ) as response:
                        response.raise_for_status()

                        async for chunk in response.aiter_bytes(chunk_size=8192):
                            f.write(chunk)
                            total_bytes += len(chunk)
        except Exception as e:
            # We created this path, so a failed download leaves a truncated or
            # empty file that a later reader could mistake for real content.
            try:
                os.unlink(save_path)
            except OSError:
                pass
            return f"Error downloading file: {str(e)}"

        size_str = format_file_size(total_bytes)
        course_display = await get_course_code(course_id) or course_identifier

        # Filename is uploader-controlled (issue 239); the on-disk path uses
        # the sanitized value, only the display is fenced.
        result = f"Downloaded: {fence_untrusted_inline(filename, 'file name')}\n"
        result += f"  Path: {save_path}\n"
        result += f"  Size: {size_str}\n"
        result += f"  Type: {content_type}\n"
        result += f"  Course: {course_display}\n"
        return result

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    @validate_params
    async def read_course_file(
        course_identifier: str | int,
        file_id: str | int,
        max_size_mb: float = 25.0,
    ) -> str:
        """Read a file from a Canvas course and return its content as base64.

        Unlike download_course_file which saves to the server's local filesystem,
        this tool returns the file content directly in the response. This is useful
        when the MCP server runs on a different machine than the client.

        Use list_course_files or list_module_items to find file IDs.

        Args:
            course_identifier: Course code or Canvas ID
            file_id: Canvas file ID
            max_size_mb: Maximum file size in MB to read (default: 25). Clamped server-side to
                READ_FILE_MAX_SIZE_MB (default 100). Files larger than the effective limit are
                rejected to avoid excessive memory usage.
        """
        if max_size_mb <= 0:
            return (
                f"Error: max_size_mb must be positive (got {max_size_mb}). "
                f"Pass a value like 25 for a 25 MB limit."
            )

        server_max_mb = get_config().read_file_max_size_mb
        effective_max_mb = min(float(max_size_mb), server_max_mb)
        max_size_bytes = int(effective_max_mb * 1024 * 1024)

        course_id = await get_course_id(course_identifier)

        # Get file metadata from Canvas API
        file_info = await make_canvas_request(
            "get",
            canvas_path('courses', course_id, 'files', file_id)
        )

        if isinstance(file_info, dict) and "error" in file_info:
            return f"Error getting file info: {file_info['error']}"

        raw_filename = file_info.get("display_name") or file_info.get("filename", f"file_{file_id}")
        filename = sanitize_filename(raw_filename)
        download_url = file_info.get("url")
        content_type = file_info.get("content-type", "unknown")
        reported_size = file_info.get("size", 0)

        if not download_url:
            return "Error: No download URL available for this file. Check permissions."

        # Check reported file size before downloading
        if reported_size and reported_size > max_size_bytes:
            return (
                f"Error: File '{filename}' is {format_file_size(reported_size)}, "
                f"which exceeds the {effective_max_mb} MB limit. "
                f"Use download_course_file instead for large files."
            )

        # Download the file content into memory
        try:
            buffer = bytearray()
            async with canvas_authenticated_client() as client:
                async with client.stream("GET", download_url, follow_redirects=True) as response:
                    response.raise_for_status()

                    async for chunk in response.aiter_bytes(chunk_size=8192):
                        if len(buffer) + len(chunk) > max_size_bytes:
                            return (
                                f"Error: File '{filename}' exceeds the {effective_max_mb} MB limit "
                                f"during download. Use download_course_file instead for large files."
                            )
                        buffer.extend(chunk)

            base64_content = base64.b64encode(buffer).decode("ascii")

            size_str = format_file_size(len(buffer))
            course_display = await get_course_code(course_id) or course_identifier

            result = f"Read: {fence_untrusted_inline(filename, 'file name')}\n"
            result += f"  Size: {size_str}\n"
            result += f"  Type: {content_type}\n"
            result += f"  Course: {course_display}\n"
            result += "  Encoding: base64\n"
            result += f"  Content:\n{base64_content}\n"
            return result

        except Exception as e:
            return f"Error reading file: {str(e)}"

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    @validate_params
    async def list_course_files(
        course_identifier: str | int,
        search_term: str | None = None,
        sort: str = "updated_at",
        order: str = "desc",
    ) -> str:
        """List files in a Canvas course with optional search.

        Args:
            course_identifier: Course code or Canvas ID
            search_term: Filter files by name
            sort: Sort field: name, size, created_at, updated_at, content_type (default: updated_at)
            order: "asc" or "desc" (default: desc)
        """
        # Validate sort and order parameters
        valid_sort_fields = {"name", "size", "created_at", "updated_at", "content_type"}
        if sort not in valid_sort_fields:
            return f"Invalid sort field: '{sort}'. Must be one of: {', '.join(sorted(valid_sort_fields))}"

        if order not in ("asc", "desc"):
            return f"Invalid order: '{order}'. Must be 'asc' or 'desc'."

        course_id = await get_course_id(course_identifier)

        params = {
            "per_page": 100,
            "sort": sort,
            "order": order,
        }
        if search_term:
            params["search_term"] = search_term

        files = await fetch_all_paginated_results(
            canvas_path('courses', course_id, 'files'),
            params
        )

        if isinstance(files, dict) and "error" in files:
            return f"Error listing files: {files['error']}"

        if not files:
            msg = "No files found"
            if search_term:
                msg += f" matching '{search_term}'"
            return msg

        course_display = await get_course_code(course_id) or course_identifier
        result = f"Files in {course_display}:\n\n"

        for f in files:
            fid = f.get("id", "?")
            name = f.get("display_name") or f.get("filename", "unknown")
            size = format_file_size(f.get("size", 0))
            ctype = f.get("content-type", "unknown")
            result += f"  ID: {fid} | {fence_untrusted_inline(name, 'file name')} ({size}, {ctype})\n"

        result += f"\nTotal: {len(files)} file(s)"
        return result


def register_educator_file_tools(mcp: FastMCP) -> None:
    """Register educator-only file tools."""

    @mcp.tool(annotations=ToolAnnotations(destructive_hint=True, idempotent_hint=True))
    @validate_params
    async def delete_course_folder(
        course_identifier: str | int,
        folder_id: str | int,
        confirmation_token: str | None = None,
    ) -> str:
        """Preview and delete an empty course folder; root and recursive deletion are refused.

        Delete contained files individually using their guarded tools first.
        Canvas is instructed to refuse if content appears after the preview.
        """
        course_id = await _folder_course_id(course_identifier)
        if isinstance(course_id, dict):
            return f"Error: {course_id['error']}"
        folder = await _get_course_folder(course_id, folder_id)
        if "error" in folder:
            return f"Error: {folder['error']}"
        if not folder.get("parent_folder_id"):
            return "Error: The course root folder cannot be deleted."
        for collection in ("files", "folders"):
            contents = await fetch_all_paginated_results(canvas_path("folders", folder["id"], collection))
            if not isinstance(contents, list):
                return "Error: Could not verify that the folder is empty."
            if contents:
                return "Error: Only empty folders can be deleted. Remove contents using their guarded tools first."
        fingerprint = _DELETE_FOLDER_GUARD.fingerprint(
            "delete_course_folder", course_id, str(folder["id"]),
            json.dumps(folder, sort_keys=True, default=str),
        )
        if not confirmation_token:
            return preview_with_token(
                _DELETE_FOLDER_GUARD, fingerprint, "delete_course_folder",
                "Would delete this empty course folder:\n" + _format_course_folder(folder),
            )
        error = redeem_confirmation(_DELETE_FOLDER_GUARD, confirmation_token, fingerprint)
        if error:
            return error
        response = await make_canvas_request(
            "delete", canvas_path("folders", folder["id"]), params={"force": "false"},
        )
        if not isinstance(response, dict) or "error" in response:
            return "Warning: Folder deletion was not confirmed. Read the folder before retrying."
        return "Empty course folder deleted.\n" + _format_course_folder(folder)

    @mcp.tool(annotations=ToolAnnotations(destructive_hint=False, idempotent_hint=False))
    @validate_params
    async def copy_course_folder(
        course_identifier: str | int,
        source_folder_id: str | int,
        destination_folder_id: str | int,
        source_course_identifier: str | int | None = None,
    ) -> str:
        """Copy a content folder into a verified course folder, renaming collisions.

        The destination course is course_identifier. Source defaults to the
        same course. Submission folders and copying into descendants are refused.
        """
        target_course = await _folder_course_id(course_identifier)
        source_course = await _folder_course_id(
            source_course_identifier if source_course_identifier is not None else course_identifier
        )
        if isinstance(target_course, dict) or isinstance(source_course, dict):
            return "Error: Cannot resolve the source or destination course."
        source = await _get_course_folder(source_course, source_folder_id)
        target = await _get_course_folder(target_course, destination_folder_id)
        if "error" in source or "error" in target:
            return "Error: Cannot verify source and destination course-content folders."
        folders = await fetch_all_paginated_results(canvas_path("courses", source_course, "folders"))
        if not isinstance(folders, list) or any(not isinstance(item, dict) for item in folders):
            return "Error: Cannot inspect the source folder tree."
        descendants = {str(source["id"])}
        pending = [source]
        while pending:
            current = pending.pop()
            if _folder_ownership_error(current, source_course):
                return "Error: Source tree contains an unverified or submission folder."
            for child in folders:
                if str(child.get("parent_folder_id")) == str(current["id"]):
                    child_id = str(child.get("id"))
                    if child_id in descendants:
                        return "Error: Source folder tree contains duplicate or cyclic IDs."
                    descendants.add(child_id)
                    pending.append(child)
        if source_course == target_course and str(target["id"]) in descendants:
            return "Error: A folder cannot be copied into itself or a descendant."
        copied = await make_canvas_request(
            "post", canvas_path("folders", target["id"], "copy_folder"),
            data={"source_folder_id": source["id"]}, use_form_data=True,
        )
        if _folder_ownership_error(copied, target_course) or str(copied.get("parent_folder_id")) != str(target["id"]):
            return "Warning: Folder copy was not confirmed. List destination folders before retrying."
        return "Course folder copied.\n" + _format_course_folder(copied)

    @mcp.tool(annotations=ToolAnnotations(destructive_hint=False, idempotent_hint=False))
    @validate_params
    async def copy_course_file(
        course_identifier: str | int,
        source_file_id: str | int,
        destination_folder_id: str | int,
        source_course_identifier: str | int | None = None,
    ) -> str:
        """Copy a verified course file, always renaming collisions rather than overwriting."""
        target_course = await _folder_course_id(course_identifier)
        source_course = await _folder_course_id(
            source_course_identifier if source_course_identifier is not None else course_identifier
        )
        if isinstance(target_course, dict) or isinstance(source_course, dict):
            return "Error: Cannot resolve the source or destination course."
        source = await make_canvas_request("get", canvas_path("courses", source_course, "files", source_file_id))
        if not isinstance(source, dict) or "error" in source or str(source.get("id")) != str(source_file_id):
            return "Error: Cannot verify source course file."
        parent_id = source.get("folder_id")
        if not _positive_folder_id(parent_id):
            return "Error: Cannot verify source file folder."
        parent = await _get_course_folder(source_course, str(parent_id))
        target = await _get_course_folder(target_course, destination_folder_id)
        if "error" in parent or "error" in target:
            return "Error: Source and destination must be course-content folders."
        copied = await make_canvas_request(
            "post", canvas_path("folders", target["id"], "copy_file"),
            data={"source_file_id": source["id"], "on_duplicate": "rename"}, use_form_data=True,
        )
        if not isinstance(copied, dict) or not _positive_folder_id(copied.get("id")) or str(copied.get("folder_id")) != str(target["id"]):
            return "Warning: File copy was not confirmed. List destination files before retrying."
        name = fence_untrusted_inline(copied.get("display_name") or copied.get("filename") or "Unnamed", "file name")
        return f"Course file copied.\nFile ID: {copied['id']}\nFolder ID: {target['id']}\nName: {name}"

    @mcp.tool(annotations=ToolAnnotations(destructive_hint=False, idempotent_hint=False))
    @validate_params
    async def create_course_folder(
        course_identifier: str | int,
        name: str,
        parent_folder_id: str | int | None = None,
        parent_folder_path: str | None = None,
        lock_at: str | None = None,
        unlock_at: str | None = None,
        locked: bool | None = None,
        hidden: bool | None = None,
        position: int | None = None,
    ) -> str:
        """Create a course-content folder with optional availability settings.

        Args:
            course_identifier: Course code, Canvas ID, or explicit SIS ID
            name: Folder name, containing 1 to 255 characters
            parent_folder_id: Existing folder in the same course
            parent_folder_path: Course-relative parent path; missing folders are created
            lock_at: ISO 8601 scheduled lock date
            unlock_at: ISO 8601 scheduled unlock date
            locked: Lock or unlock the folder immediately
            hidden: Hide or show the folder
            position: Nonnegative sort position
        """
        fields = _folder_fields(name, lock_at, unlock_at, locked, hidden, position)
        if isinstance(fields, str):
            return fields
        if parent_folder_id is not None and parent_folder_path is not None:
            return "Invalid configuration: provide parent_folder_id or parent_folder_path, not both."
        if parent_folder_path is not None and contains_fence_markers(parent_folder_path):
            return FENCE_LEAK_ERROR
        course_id = await _folder_course_id(course_identifier)
        if isinstance(course_id, dict):
            return f"Error resolving course: {course_id['error']}"
        if parent_folder_id is not None:
            parent = await _get_course_folder(course_id, parent_folder_id)
            if "error" in parent:
                return f"Error fetching parent folder: {parent['error']}"
            fields["parent_folder_id"] = parent["id"]
        else:
            fields["parent_folder_path"] = parent_folder_path or ""
        folder = await make_canvas_request(
            "post", canvas_path("courses", course_id, "folders"), data=fields, use_form_data=True
        )
        if isinstance(folder, dict) and "error" in folder:
            if not isinstance(folder, RequestFailure) or folder.outcome == WriteOutcome.MAY_HAVE_WRITTEN:
                return unconfirmed_write_warning(
                    "whether Canvas created the course folder",
                    {"Course ID": course_id, "Detail": folder["error"]},
                    "Canvas may have created the folder. Check the course folders before retrying.",
                )
            return f"Error creating folder: {folder['error']}"
        if error := _folder_ownership_error(folder, course_id):
            return (
                f"Warning: Folder creation outcome is unconfirmed: {error} "
                "Canvas may have created the folder. Check the course folders before retrying."
            )
        return "✅ Folder created successfully!\n\n" + _format_course_folder(folder)

    @mcp.tool(annotations=ToolAnnotations(destructive_hint=True, idempotent_hint=True))
    @validate_params
    async def update_course_folder(
        course_identifier: str | int,
        folder_id: str | int,
        name: str | None = None,
        parent_folder_id: str | int | None = None,
        lock_at: str | None = None,
        unlock_at: str | None = None,
        locked: bool | None = None,
        hidden: bool | None = None,
        position: int | None = None,
        clear_lock_at: bool = False,
        clear_unlock_at: bool = False,
    ) -> str:
        """Rename, move, or change availability settings for a course folder.

        Args:
            course_identifier: Course code, Canvas ID, or explicit SIS ID
            folder_id: Canvas folder ID or 'root'
            name: New folder name, containing 1 to 255 characters
            parent_folder_id: Move into an existing folder in the same course
            lock_at: ISO 8601 scheduled lock date
            unlock_at: ISO 8601 scheduled unlock date
            locked: Lock or unlock the folder immediately
            hidden: Hide or show the folder
            position: Nonnegative sort position
            clear_lock_at: Remove the scheduled lock date
            clear_unlock_at: Remove the scheduled unlock date
        """
        fields = _folder_fields(
            name, lock_at, unlock_at, locked, hidden, position, clear_lock_at, clear_unlock_at
        )
        if isinstance(fields, str):
            return fields
        if not fields and parent_folder_id is None:
            return "Error: No fields provided to update."
        course_id = await _folder_course_id(course_identifier)
        if isinstance(course_id, dict):
            return f"Error resolving course: {course_id['error']}"
        existing = await _get_course_folder(course_id, folder_id)
        if "error" in existing:
            return f"Error fetching folder: {existing['error']}"
        if parent_folder_id is not None:
            parent = await _get_course_folder(course_id, parent_folder_id)
            if "error" in parent:
                return f"Error fetching parent folder: {parent['error']}"
            if str(parent["id"]) == str(existing["id"]):
                return "Error: A folder cannot be its own parent."
            fields["parent_folder_id"] = parent["id"]
        folder = await make_canvas_request(
            "put", canvas_path("folders", existing["id"]), data=fields, use_form_data=True
        )
        if isinstance(folder, dict) and "error" in folder:
            if not isinstance(folder, RequestFailure) or folder.outcome == WriteOutcome.MAY_HAVE_WRITTEN:
                return unconfirmed_write_warning(
                    "whether Canvas updated the course folder",
                    {"Course ID": course_id, "Folder ID": existing["id"], "Detail": folder["error"]},
                    "Canvas may have updated the folder. Check the course folder before retrying.",
                )
            return f"Error updating folder: {folder['error']}"
        error = _folder_ownership_error(folder, course_id)
        if not error and str(folder["id"]) != str(existing["id"]):
            error = "Canvas returned a different folder ID."
        if error:
            return (
                f"Warning: Folder update outcome is unconfirmed: {error} "
                "Canvas may have updated the folder. Check the course folder before retrying."
            )
        return "✅ Folder updated successfully!\n\n" + _format_course_folder(folder)


    @mcp.tool(annotations=ToolAnnotations(destructive_hint=True, idempotent_hint=True))
    @validate_params
    async def update_course_file(
        course_identifier: str | int,
        file_id: str | int,
        name: str | None = None,
        parent_folder_id: str | int | None = None,
        on_duplicate: str | None = None,
        lock_at: str | None = None,
        unlock_at: str | None = None,
        locked: bool | None = None,
        hidden: bool | None = None,
        visibility_level: str | None = None,
        clear_lock_at: bool = False,
        clear_unlock_at: bool = False,
    ) -> str:
        """Rename, move, or change visibility and lock settings for a course file.

        This updates file metadata only; it does not replace the file contents.

        Args:
            course_identifier: Course code or Canvas ID
            file_id: Canvas file ID
            name: New display name (maximum 255 characters)
            parent_folder_id: Folder ID to move the file into (same course)
            on_duplicate: How to handle a name collision: "overwrite" or "rename"
            lock_at: Date/time at which the file becomes locked
            unlock_at: Date/time at which the file becomes available
            locked: Lock or unlock the file immediately
            hidden: Hide or show the file
            visibility_level: One of inherit, course, institution, or public
            clear_lock_at: Remove the scheduled lock date
            clear_unlock_at: Remove the scheduled unlock date
        """
        if name is not None and contains_fence_markers(name):
            return FENCE_LEAK_ERROR
        if name is not None and (not name.strip() or len(name) > 255):
            return "Invalid name: file names must contain 1 to 255 characters."
        if on_duplicate is not None and on_duplicate not in {"overwrite", "rename"}:
            return "Invalid on_duplicate value. Must be 'overwrite' or 'rename'."
        valid_visibility = {"inherit", "course", "institution", "public"}
        if visibility_level is not None and visibility_level not in valid_visibility:
            return f"Invalid visibility_level. Must be one of: {', '.join(sorted(valid_visibility))}."
        for field, value, clear in (
            ("lock_at", lock_at, clear_lock_at),
            ("unlock_at", unlock_at, clear_unlock_at),
        ):
            if value is not None and clear:
                return f"Invalid configuration: {field} and clear_{field} cannot both be provided."

        updates: dict[str, Any] = {}
        if name is not None:
            updates["name"] = name
        if parent_folder_id is not None:
            updates["parent_folder_id"] = parent_folder_id
        if on_duplicate is not None:
            updates["on_duplicate"] = on_duplicate
        for field, value, clear in (
            ("lock_at", lock_at, clear_lock_at),
            ("unlock_at", unlock_at, clear_unlock_at),
        ):
            if clear:
                updates[field] = ""
            elif value is not None:
                parsed = parse_date(value)
                if parsed is None:
                    return f"Invalid date format for {field}: '{value}'. Use ISO 8601 format."
                updates[field] = parsed.isoformat()
        if locked is not None:
            updates["locked"] = locked
        if hidden is not None:
            updates["hidden"] = hidden
        if visibility_level is not None:
            updates["visibility_level"] = visibility_level
        if not updates:
            return "Error: No fields provided to update."

        course_id = await get_course_id(course_identifier)
        existing = await make_canvas_request(
            "get", canvas_path("courses", course_id, "files", file_id)
        )
        if isinstance(existing, dict) and "error" in existing:
            return f"Error fetching file details: {existing['error']}"
        response = await make_canvas_request(
            "put", canvas_path("files", file_id), data=updates, use_form_data=True
        )
        if isinstance(response, dict) and "error" in response:
            return f"Error updating file: {response['error']}"

        file_name = response.get("display_name") or response.get(
            "filename", name or "Unknown"
        )
        course_display = await get_course_code(course_id) or course_identifier
        result = "✅ File updated successfully!\n\n"
        result += f"  File: {fence_untrusted_inline(file_name, 'file name')}\n"
        result += f"  File ID: {response.get('id', file_id)}\n"
        result += f"  Course: {course_display}\n"
        if response.get("folder_id") is not None:
            result += f"  Folder ID: {response['folder_id']}\n"
        return result

    @mcp.tool(annotations=ToolAnnotations(destructive_hint=True, idempotent_hint=True))
    @validate_params
    async def delete_course_file(
        course_identifier: str | int,
        file_id: str | int,
        require_name_match: str | None = None,
        allow_deleting_module_references: bool = False,
        confirmation_token: str | None = None,
    ) -> str:
        """Permanently delete a course file after a preview and confirmation.

        Canvas does not remove module items that link to a deleted file. This
        tool reports those links and refuses unless they have first been
        removed or allow_deleting_module_references is explicitly true.

        Args:
            course_identifier: Course code or Canvas ID
            file_id: Canvas file ID
            require_name_match: Only delete if the current display name matches exactly
            allow_deleting_module_references: Permit deletion despite linked module items
            confirmation_token: Token from the preview call; omit to preview
        """
        course_id = await get_course_id(course_identifier)
        file_info = await make_canvas_request(
            "get", canvas_path("courses", course_id, "files", file_id)
        )
        if isinstance(file_info, dict) and "error" in file_info:
            return f"Error fetching file details: {file_info['error']}"

        file_name = file_info.get("display_name") or file_info.get(
            "filename", "Unknown"
        )
        if require_name_match is not None and file_name != require_name_match:
            return (
                f"Error: File name is {fence_untrusted_inline(file_name, 'file name')}, "
                f"not the required name {fence_untrusted_inline(require_name_match, 'required file name')}. "
                "Nothing was deleted."
            )

        references = await _file_module_references(course_id, file_id)
        if isinstance(references, dict):
            return f"Error checking module references: {references['error']}. Nothing was deleted."
        if references and not allow_deleting_module_references:
            shown = ", ".join(
                f"{fence_untrusted_inline(ref['module_name'], 'module name')} / "
                f"{fence_untrusted_inline(ref['item_title'], 'module item title')} "
                f"(item {ref['item_id']})"
                for ref in references
            )
            return (
                f"Error: File {fence_untrusted_inline(file_name, 'file name')} is linked from "
                f"{len(references)} module item(s): {shown}. Canvas would leave broken module "
                "items. Remove them first, or pass allow_deleting_module_references=true to "
                "explicitly permit this. Nothing was deleted."
            )

        reference_facts = json.dumps(references, sort_keys=True, separators=(",", ":"))
        fingerprint = _DELETE_FILE_GUARD.fingerprint(
            "delete_course_file",
            str(course_id),
            str(file_id),
            file_name,
            str(file_info.get("folder_id")),
            str(file_info.get("size")),
            str(file_info.get("updated_at")),
            str(require_name_match),
            str(allow_deleting_module_references),
            reference_facts,
        )
        shown_name = fence_untrusted_inline(file_name, "file name")
        course_display = await get_course_code(course_id) or course_identifier
        if not confirmation_token:
            preview = (
                f"Would permanently delete file **{shown_name}** from course {course_display}\n"
                f"  File ID: {file_id}\n"
                f"  Folder ID: {file_info.get('folder_id', 'Unknown')}\n"
                f"  Size: {format_file_size(file_info.get('size', 0))}\n"
                f"  Module references left broken: {len(references)}"
            )
            if references:
                for ref in references:
                    preview += (
                        f"\n    - {fence_untrusted_inline(ref['module_name'], 'module name')} / "
                        f"{fence_untrusted_inline(ref['item_title'], 'module item title')} "
                        f"(module {ref['module_id']}, item {ref['item_id']})"
                    )
            return preview_with_token(
                _DELETE_FILE_GUARD, fingerprint, "delete_course_file", preview
            )

        error = redeem_confirmation(_DELETE_FILE_GUARD, confirmation_token, fingerprint)
        if error:
            return error
        response = await make_canvas_request("delete", canvas_path("files", file_id))
        if isinstance(response, dict) and "error" in response:
            return f"Error deleting file: {response['error']}"
        return (
            "✅ File deleted successfully!\n\n"
            f"  Deleted: **{shown_name}**\n"
            f"  Course: {course_display}\n"
            f"  File ID: {file_id}\n"
            f"  Module references left broken: {len(references)}\n"
        )

    @mcp.tool(annotations=ToolAnnotations(destructive_hint=True, idempotent_hint=False))
    @validate_params
    async def upload_course_file(
        course_identifier: str | int,
        file_path: str,
        folder_path: str | None = None,
        display_name: str | None = None,
        on_duplicate: str = "rename"
    ) -> str:
        """Upload a file to Canvas course storage.

        Uploads a local file to a Canvas course. The returned file ID can be used with
        add_module_item (item_type='File') or send_conversation (attachment_ids).

        Args:
            course_identifier: Course code or Canvas ID
            file_path: Absolute path to the local file to upload
            folder_path: Canvas folder path (default: "course files" root)
            display_name: Override the filename shown in Canvas
            on_duplicate: "rename" (default) or "overwrite"
        """
        # 'file_path' reads the *server's* filesystem. On a local stdio server that
        # is the caller's own machine; on a shared HTTP one a remote caller could
        # name any file the service account can read and upload it into their own
        # Canvas course. Refused outright over HTTP, matching the student upload
        # path in student_write.py, which already blocks the same hole.
        if is_http_request_active():
            return (
                "Error: 'file_path' reads files from the server and is only "
                "available on a local (stdio) server. On this hosted server, "
                "upload the file through Canvas directly."
            )

        # Validate on_duplicate parameter
        if on_duplicate not in ("rename", "overwrite"):
            return f"Invalid on_duplicate value: '{on_duplicate}'. Must be 'rename' or 'overwrite'."

        # Step 0: Validate the file locally first
        validation: FileValidationResult = validate_file_for_upload(file_path)

        if not validation.valid:
            return f"❌ File validation failed: {validation.error}"

        # Get course ID for API calls
        course_id = await get_course_id(course_identifier)

        # Determine the filename to use in Canvas
        upload_filename = display_name if display_name else validation.sanitized_name

        # Step 1: Request upload URL from Canvas API
        upload_request_params = {
            "name": upload_filename,
            "size": validation.file_size,
            "content_type": validation.mime_type,
            "on_duplicate": on_duplicate,
        }

        # Canvas expects the folder path relative to course files. ALWAYS send
        # it: omitting parent_folder_path does not mean "root", it means Canvas
        # creates and uses a folder literally named "unfiled" (issue #198,
        # reproduced live — A/B: no param -> "course files/unfiled";
        # parent_folder_path="" -> "course files"). Empty string is the root, and
        # costs no extra request, unlike looking up /folders/root for its id.
        upload_request_params["parent_folder_path"] = folder_path or ""

        # Request the upload slot
        step1_response = await make_canvas_request(
            "post",
            canvas_path('courses', course_id, 'files'),
            data=upload_request_params,
            use_form_data=True
        )

        if isinstance(step1_response, dict) and "error" in step1_response:
            return f"❌ Failed to request upload URL: {step1_response['error']}"

        # Extract upload URL and parameters
        upload_url = step1_response.get("upload_url")
        upload_params = step1_response.get("upload_params", {})

        if not upload_url:
            return "❌ Canvas API did not return an upload URL. Check API permissions."

        # Step 2: Upload file to external storage
        step2_response = await upload_file_to_storage(
            upload_url=upload_url,
            upload_params=upload_params,
            file_path=file_path,
            filename=upload_filename,
            content_type=validation.mime_type
        )

        if isinstance(step2_response, dict) and "error" in step2_response:
            error_msg = step2_response.get("error", "Unknown error")
            details = step2_response.get("details", "")
            if details:
                return f"❌ File upload failed: {error_msg}\nDetails: {details}"
            return f"❌ File upload failed: {error_msg}"

        # Step 3: Extract file information from response
        # The response could be from:
        # - Direct storage response (200/201)
        # - Redirect confirmation from Canvas API

        file_id = step2_response.get("id")
        file_name = step2_response.get("display_name") or step2_response.get("filename") or upload_filename
        file_url = step2_response.get("url", "")
        file_folder_id = step2_response.get("folder_id")

        # If we got a success but no file ID, the file might need confirmation
        # This can happen with some storage backends
        if not file_id and step2_response.get("success"):
            # Try to find the file by name in the course
            # This is a fallback for edge cases
            return (
                "⚠️ Upload appears successful but file ID not returned. "
                "The file may need manual verification in Canvas."
            )

        if not file_id:
            return (
                "❌ Upload completed but no file ID received. "
                f"Response: {step2_response}"
            )

        # Format success response
        course_display = await get_course_code(course_id) or course_identifier
        file_size_str = format_file_size(validation.file_size)

        result = "✅ File uploaded successfully!\n\n"
        result += f"**{file_name}**\n"
        result += f"  File ID: {file_id}\n"
        result += f"  Course: {course_display}\n"
        result += f"  Size: {file_size_str}\n"
        result += f"  Type: {validation.mime_type}\n"

        if file_folder_id:
            result += f"  Folder ID: {file_folder_id}\n"

        if folder_path:
            result += f"  Folder Path: {folder_path}\n"

        result += "\n**Next steps:**\n"
        result += f"  - Add to module: add_module_item(..., item_type='File', content_id={file_id})\n"
        result += f"  - Attach to message: send_conversation(..., attachment_ids=['{file_id}'])\n"

        if file_url:
            result += f"  - Direct URL: {file_url}\n"

        return result
