"""Confirmed local Common Cartridge imports with scoped upload handling."""

from __future__ import annotations

import hashlib
import io
import ipaddress
import json
import re
import stat
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx
from fastmcp import FastMCP
from mcp.types import ToolAnnotations

from ..core.client import canvas_authenticated_client
from ..core.config import get_config
from ..core.credentials import get_request_credentials, is_http_request_active
from ..core.path import canvas_path
from ..core.untrusted_content import contains_fence_markers, fence_untrusted_inline
from ..core.validation import coerce_canvas_id, validate_params
from ..core.write_confirmation import ConfirmationGuard, redeem_confirmation
from .content_migrations import _next_status_action, _resolve_course, _target_occupancy

_IMPORT_GUARD = ConfirmationGuard(nothing_done="The archive was not imported.")
_MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
_MAX_EXPANDED_BYTES = 2 * 1024 * 1024 * 1024
_MAX_ARCHIVE_ENTRIES = 20000
_IMPORT_TYPE = "common_cartridge_importer"
_S3_UPLOAD_HOST = re.compile(
    r"(?:[a-z0-9][a-z0-9.-]*\.)?s3(?:[.-][a-z0-9-]+)?\.amazonaws\.com(?:\.cn)?"
)
_FILE_CONFIRM_PATH = re.compile(r"/api/v[0-9]+/files/[0-9]+(?:/create_success)?")


def _archive_snapshot(file_path: str) -> tuple[Path, bytes, str]:
    if contains_fence_markers(file_path):
        raise ValueError("Archive paths must not contain untrusted-content markers.")
    path = Path(file_path).expanduser().resolve(strict=True)
    if path.suffix.lower() != ".imscc" or not path.is_file():
        raise ValueError("Choose a regular local .imscc archive.")
    with path.open("rb") as archive_file:
        content = archive_file.read(_MAX_ARCHIVE_BYTES + 1)
    if len(content) > _MAX_ARCHIVE_BYTES:
        raise ValueError("The archive exceeds the 256 MiB local import limit.")
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            entries = archive.infolist()
            if len(entries) > _MAX_ARCHIVE_ENTRIES or sum(entry.file_size for entry in entries) > _MAX_EXPANDED_BYTES:
                raise ValueError("The archive exceeds the entry or expanded-size limit.")
            if "imsmanifest.xml" not in archive.namelist():
                raise ValueError("The archive must contain a root imsmanifest.xml.")
            for entry in entries:
                name = entry.filename
                path_parts = PurePosixPath(name).parts
                if name.startswith("/") or "\\" in name or ".." in path_parts or ":" in name:
                    raise ValueError("Archive entries must use safe relative paths.")
                if entry.flag_bits & 1 or stat.S_ISLNK(entry.external_attr >> 16):
                    raise ValueError("Encrypted archives and symlink entries are not supported.")
    except zipfile.BadZipFile as exc:
        raise ValueError("The file is not a valid Common Cartridge ZIP archive.") from exc
    return path, content, hashlib.sha256(content).hexdigest()


def _https_url(value: object) -> httpx.URL | None:
    if not isinstance(value, str) or any(ord(char) < 32 for char in value):
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
            return None
        if parsed.port not in (None, 443):
            return None
        hostname = parsed.hostname.lower().rstrip(".")
        if hostname == "localhost" or hostname.endswith((".localhost", ".local")):
            return None
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            return None
        return httpx.URL(value)
    except (ValueError, httpx.InvalidURL):
        return None


def _canvas_api_base() -> str:
    raw = get_config().canvas_api_url.rstrip("/")
    url = _https_url(raw)
    if url is None or url.query or not re.fullmatch(r"/api/v[0-9]+", url.path):
        raise ValueError("Local archive imports require a configured HTTPS Canvas REST API root.")
    return raw


def _storage_url(value: object, api_base: str) -> httpx.URL | None:
    target = _https_url(value)
    canvas = httpx.URL(api_base)
    if target is None:
        return None
    same_canvas = (target.scheme, target.host, target.port) == (canvas.scheme, canvas.host, canvas.port)
    if not same_canvas and not _S3_UPLOAD_HOST.fullmatch(target.host):
        return None
    return target


def _confirmation_url(location: object, upload_url: str, api_base: str) -> str | None:
    if not isinstance(location, str):
        return None
    candidate = _https_url(urljoin(upload_url, location))
    canvas = httpx.URL(api_base)
    if candidate is None or (candidate.scheme, candidate.host, candidate.port) != (canvas.scheme, canvas.host, canvas.port):
        return None
    if not _FILE_CONFIRM_PATH.fullmatch(candidate.path):
        return None
    if not candidate.path.startswith(canvas.path + "/files/"):
        return None
    return str(candidate)


async def _create_import(api_base: str, course_id: str, filename: str, size: int) -> dict[str, Any]:
    async with canvas_authenticated_client() as client:
        response = await client.post(
            api_base + canvas_path("courses", course_id, "content_migrations"),
            data={
                "migration_type": _IMPORT_TYPE,
                "selective_import": "false",
                "pre_attachment[name]": filename,
                "pre_attachment[size]": str(size),
                "pre_attachment[content_type]": "application/zip",
            },
            follow_redirects=False,
        )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("Invalid migration response.")
    return payload


async def _upload_archive(slot: object, api_base: str, filename: str, content: bytes) -> bool:
    if not isinstance(slot, dict):
        return False
    upload_url = slot.get("upload_url")
    params = slot.get("upload_params")
    if not isinstance(upload_url, str) or _storage_url(upload_url, api_base) is None or not isinstance(params, dict) or not params or "file" in params:
        return False
    if any(not isinstance(key, str) or not isinstance(value, (str, int, float, bool)) for key, value in params.items()):
        return False
    async with httpx.AsyncClient(timeout=get_config().api_timeout, trust_env=False) as storage:
        uploaded = await storage.post(
            upload_url, data=params,
            files={"file": (filename, content, "application/zip")},
            follow_redirects=False,
        )
    if uploaded.status_code not in (200, 201, 301, 302, 303):
        return False
    location = uploaded.headers.get("Location")
    if location is not None:
        confirmation_url = _confirmation_url(location, str(upload_url), api_base)
        if confirmation_url is None:
            return False
        async with canvas_authenticated_client() as client:
            confirmed = await client.get(confirmation_url, follow_redirects=False)
        if confirmed.status_code != 200:
            return False
        payload = confirmed.json()
    elif uploaded.status_code in (200, 201):
        payload = uploaded.json()
    else:
        return False
    return isinstance(payload, dict) and coerce_canvas_id(payload.get("id", "")) is not None


def register_content_import_tools(mcp: FastMCP) -> None:
    """Register local-only archive import with preview and confirmation."""

    @mcp.tool(annotations=ToolAnnotations(destructive_hint=True, idempotent_hint=False))
    @validate_params
    async def import_course_content(
        course_identifier: str | int,
        file_path: str,
        confirmation_token: str | None = None,
    ) -> dict[str, Any]:
        """Preview and confirm a full import of a local .imscc archive.

        Available only in local stdio mode. Preview binds archive SHA-256,
        size, target identity and current occupancy. Unavailable occupancy
        counts are reported as partial, and a changed count voids the token.
        Confirmation uploads the
        exact validated bytes into a Canvas-issued migration slot. The archive
        limit is 256 MiB, 20,000 entries and 2 GiB expanded. Upload completion
        starts asynchronous import; inspect migration status and every issue.
        Storage must use the configured Canvas HTTPS origin or an Amazon S3
        endpoint; other storage backends are unsupported. Creation and upload
        are never automatically retried.
        """
        if is_http_request_active() or get_request_credentials() is not None:
            return {"error": "Local archive paths are unavailable over HTTP transport."}
        try:
            api_base = _canvas_api_base()
            path, content, digest = _archive_snapshot(file_path)
        except (OSError, ValueError):
            return {"error": "Choose a readable, valid .imscc archive within the documented limits and a configured HTTPS Canvas API."}
        course_id, course, error = await _resolve_course(course_identifier, "target")
        if error or course_id is None or course is None:
            return {"error": "Could not verify the target course identity."}
        occupancy = await _target_occupancy(course_id)
        identity = {key: course.get(key) for key in ("id", "name", "course_code")}
        fingerprint = _IMPORT_GUARD.fingerprint(
            "import_course_content", api_base, course_id, str(path), digest, str(len(content)),
            json.dumps(identity, sort_keys=True), json.dumps(occupancy, sort_keys=True),
        )
        if not confirmation_token:
            warning = "Import adds content and can create duplicates or alter existing content; occupancy is not a collision diff."
            if occupancy.get("unavailable"):
                warning += " Some target content counts are unavailable, so occupancy is partial."
            return {
                "preview": True, "migration_requested": False,
                "target_course_id": course_id,
                "target_course_name": fence_untrusted_inline(course.get("name") or "Unnamed course", "course name"),
                "target_course_code": course.get("course_code"),
                "filename": fence_untrusted_inline(path.name, "local archive filename"),
                "archive_sha256": digest, "archive_size_bytes": len(content),
                "migration_type": _IMPORT_TYPE, "selective_import": False,
                "target_current_contents": occupancy,
                "warning": warning,
                "confirmation_token": _IMPORT_GUARD.issue(fingerprint),
                "instructions": "Show this preview to the educator, then confirm only after approval using the same arguments and confirmation_token.",
            }
        token_error = redeem_confirmation(_IMPORT_GUARD, confirmation_token, fingerprint)
        if token_error:
            return {"error": token_error, "migration_requested": False}
        try:
            created = await _create_import(api_base, course_id, path.name, len(content))
        except Exception:
            return {
                "error": "Migration creation could not be confirmed. Inspect target migration history before retrying; a retry may duplicate an import.",
                "migration_start_unconfirmed": True, "target_course_id": course_id,
                "next_action": {"tool": "list_content_migrations", "arguments": {"course_identifier": course_id}},
            }
        migration_id = coerce_canvas_id(created.get("id", ""))
        if migration_id is None:
            return {
                "error": "Canvas did not return a migration ID. Inspect target migration history before retrying.",
                "migration_start_unconfirmed": True, "target_course_id": course_id,
                "next_action": {"tool": "list_content_migrations", "arguments": {"course_identifier": course_id}},
            }
        result: dict[str, Any] = {
            "migration_created": True, "migration_id": migration_id,
            "target_course_id": course_id, "archive_sha256": digest,
            "archive_size_bytes": len(content),
            "next_action": _next_status_action(course_id, migration_id),
        }
        try:
            uploaded = await _upload_archive(created.get("pre_attachment"), api_base, path.name, content)
        except Exception:
            uploaded = False
        result["upload_confirmed"] = uploaded
        result["import_complete"] = False
        if not uploaded:
            result["import_start_unconfirmed"] = True
            result["warning"] = "Migration exists, but upload completion could not be confirmed. Inspect its status and migration history before retrying; no automatic retry was attempted."
        return result
