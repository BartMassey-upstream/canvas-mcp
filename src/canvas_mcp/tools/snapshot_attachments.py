"""Opt-in private attachment downloads bound to verified snapshot evidence."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import logging
import os
import re
import socket
import uuid
from contextvars import ContextVar
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpcore
import httpx
from fastmcp import FastMCP
from mcp.types import ToolAnnotations

from ..core.artifact_store import (
    MAX_ARCHIVE_BYTES,
    MAX_FILE_BYTES,
    MAX_MANIFEST_BYTES,
    ArtifactStore,
    canonical_origin,
    json_bytes,
)
from ..core.client import make_canvas_request
from ..core.config import get_config
from ..core.credentials import is_http_request_active
from ..core.path import canvas_path
from ..core.record_snapshots import now, numeric_id, verified_records, verify_snapshot
from ..core.write_confirmation import ConfirmationGuard, redeem_confirmation
from .record_snapshots import _capture_context

_GUARD = ConfirmationGuard(nothing_done="No attachments were downloaded.")
_S3_HOST = re.compile(
    r"(?:[a-z0-9][a-z0-9.-]*\.)?s3(?:[.-][a-z0-9-]+)?\.amazonaws\.com(?:\.cn)?"
)
_DOWNLOAD_ACTIVE: ContextVar[bool] = ContextVar(
    "snapshot_attachment_download", default=False
)


class _DownloadLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not _DOWNLOAD_ACTIVE.get()


_LOG_FILTER = _DownloadLogFilter()
for _logger_name in ("httpcore.connection", "httpcore.http11"):
    logging.getLogger(_logger_name).addFilter(_LOG_FILTER)


class DownloadRejected(ValueError):
    """Carry only a fixed, non-sensitive reason code."""


async def _public_ip(host: str, port: int) -> str:
    addresses = await asyncio.wait_for(
        asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM),
        timeout=float(get_config().api_timeout),
    )
    ips: set[str] = set()
    for item in addresses:
        address = item[4][0]
        if not isinstance(address, str):
            raise DownloadRejected("unsafe_download_address")
        ips.add(address)
    if not ips or any(not ipaddress.ip_address(value).is_global for value in ips):
        raise DownloadRejected("unsafe_download_address")
    return sorted(ips)[0]


def _download_target(url: object, origin: str) -> tuple[str, str, bool]:
    if not isinstance(url, str) or any(
        char.isspace() or ord(char) < 32 for char in url
    ):
        raise DownloadRejected("unsafe_download_url")
    try:
        target = urlsplit(url)
        if (
            target.scheme != "https"
            or target.port not in {None, 443}
            or target.username
            or target.password
            or target.fragment
            or not target.hostname
            or not target.path
        ):
            raise DownloadRejected("unsafe_download_url")
        hostname = target.hostname.lower()
        canvas = canonical_origin(url) == origin
        if not canvas and _S3_HOST.fullmatch(hostname) is None:
            raise DownloadRejected("unsupported_download_host")
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            raise DownloadRejected("unsafe_download_address")
        return url, hostname, canvas
    except ValueError as error:
        if isinstance(error, DownloadRejected):
            raise
        raise DownloadRejected("unsafe_download_url") from None


async def _download_member(
    store: ArtifactStore,
    name: str,
    url: object,
    origin: str,
    limit: int,
    expected_bytes: int | None,
) -> dict[str, Any]:
    token = _DOWNLOAD_ACTIVE.set(True)
    temporary = f"pending-{uuid.uuid4().hex}.tmp"
    try:
        external_seen = False
        for redirect in range(4):
            url, hostname, canvas = _download_target(url, origin)
            if external_seen and canvas:
                raise DownloadRejected("unsafe_redirect")
            external_seen = external_seen or not canvas
            address = await _public_ip(hostname, 443)
            pinned = str(httpx.URL(url).copy_with(host=address))
            headers = {"Host": hostname, "Accept-Encoding": "identity"}
            if canvas:
                headers["Authorization"] = "Bearer " + get_config().canvas_api_token
            timeout = float(get_config().api_timeout)
            extensions = {
                "sni_hostname": hostname,
                "timeout": dict.fromkeys(("connect", "read", "write", "pool"), timeout),
            }
            async with httpcore.AsyncConnectionPool(
                max_connections=1, retries=0
            ) as pool:
                async with pool.stream(
                    "GET", pinned, headers=list(headers.items()), extensions=extensions
                ) as response:
                    response_headers = httpx.Headers(response.headers)
                    if response.status in {301, 302, 303, 307, 308}:
                        location = response_headers.get("location")
                        if redirect >= 3 or not location:
                            raise DownloadRejected("redirect_limit_exceeded")
                        url = urljoin(url, location)
                        continue
                    if response.status != 200:
                        raise DownloadRejected("download_rejected")
                    if (
                        response_headers.get("content-encoding", "identity").lower()
                        != "identity"
                    ):
                        raise DownloadRejected("unsupported_content_encoding")
                    advertised = response_headers.get("content-length")
                    if advertised is not None and (
                        not advertised.isascii()
                        or not advertised.isdigit()
                        or int(advertised) > limit
                    ):
                        raise DownloadRejected("byte_limit_exceeded")
                    fd = os.open(
                        temporary,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                        0o600,
                        dir_fd=store.fd,
                    )
                    digest = hashlib.sha256()
                    size = 0
                    with os.fdopen(fd, "wb") as stream:
                        os.fchmod(stream.fileno(), 0o600)
                        async for chunk in response.aiter_stream():
                            size += len(chunk)
                            if size > limit:
                                raise DownloadRejected("byte_limit_exceeded")
                            stream.write(chunk)
                            digest.update(chunk)
                        if advertised is not None and size != int(advertised):
                            raise DownloadRejected("download_size_changed")
                        if expected_bytes is not None and size != expected_bytes:
                            raise DownloadRejected("attachment_size_changed")
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.link(
                        temporary,
                        ArtifactStore._name(name),
                        src_dir_fd=store.fd,
                        dst_dir_fd=store.fd,
                        follow_symlinks=False,
                    )
                    os.unlink(temporary, dir_fd=store.fd)
                    os.fsync(store.fd)
                    return {"bytes": size, "sha256": digest.hexdigest()}
        raise DownloadRejected("redirect_limit_exceeded")
    finally:
        try:
            os.unlink(temporary, dir_fd=store.fd)
        except FileNotFoundError:
            pass
        finally:
            _DOWNLOAD_ACTIVE.reset(token)


def _selected_references(
    store: ArtifactStore,
    manifest: dict[str, Any],
    selected: list[dict[str, Any]],
    max_file_bytes: int,
    max_total_bytes: int,
) -> list[dict[str, Any]]:
    if not isinstance(selected, list) or not 1 <= len(selected) <= 20:
        raise ValueError("Select between 1 and 20 attachments")
    normalized: list[dict[str, Any]] = []
    for item in selected:
        if not isinstance(item, dict) or set(item) != {
            "assignment_id",
            "user_id",
            "file_id",
        }:
            raise ValueError("Invalid selected attachment reference")
        normalized.append({key: numeric_id(value) for key, value in item.items()})
    identities = [
        (item["assignment_id"], item["user_id"], item["file_id"]) for item in normalized
    ]
    if len(identities) != len(set(identities)):
        raise ValueError("Duplicate selected attachment reference")
    found: dict[tuple[str, str, str], dict[str, Any]] = {}
    wanted = set(identities)
    for unit in manifest["units"].values():
        if unit["family"] != "submissions":
            continue
        for filename in unit["files"]:
            for row in verified_records(store, filename, manifest["files"][filename]):
                for attachment in row.get("attachments", []):
                    key = (
                        numeric_id(row["assignment_id"]),
                        numeric_id(row["user_id"]),
                        numeric_id(attachment["id"]),
                    )
                    if key in wanted:
                        found[key] = attachment
    if set(found) != wanted:
        raise ValueError("Selected attachments are not in the verified snapshot")
    known_total = 0
    for item, identity in zip(normalized, identities, strict=True):
        size = found[identity].get("size")
        if size is not None and (
            type(size) is not int or size < 0 or size > max_file_bytes
        ):
            raise ValueError("Selected attachment exceeds its byte limit")
        known_total += size or 0
        item["expected_bytes"] = size
    if known_total > max_total_bytes:
        raise ValueError("Selected attachments exceed the aggregate byte limit")
    return normalized


def _validate_bundle_manifest(manifest: object) -> dict[str, Any]:
    required = {
        "schema_version",
        "kind",
        "canvas_origin",
        "course_id",
        "caller_id",
        "parent_checkpoint_sha256",
        "started_at",
        "observed_until",
        "state",
        "selections",
        "items",
        "files",
        "canvas_writes",
        "limits",
        "total_bytes",
    }
    if (
        not isinstance(manifest, dict)
        or set(manifest) != required
        or type(manifest["schema_version"]) is not int
        or manifest["schema_version"] != 1
        or manifest["kind"] != "snapshot_attachment_download"
    ):
        raise ValueError("Unsupported attachment bundle schema")
    if canonical_origin(manifest["canvas_origin"]) != manifest["canvas_origin"]:
        raise ValueError("Invalid attachment bundle origin")
    numeric_id(manifest["course_id"])
    numeric_id(manifest["caller_id"])
    if (
        not isinstance(manifest["parent_checkpoint_sha256"], str)
        or re.fullmatch(r"[a-f0-9]{64}", manifest["parent_checkpoint_sha256"]) is None
    ):
        raise ValueError("Invalid attachment parent digest")
    from datetime import datetime

    for field in ("started_at", "observed_until"):
        if (
            not isinstance(manifest[field], str)
            or datetime.fromisoformat(manifest[field]).tzinfo is None
        ):
            raise ValueError("Invalid attachment observation time")
    if (
        manifest["state"] not in {"complete", "partial", "interrupted"}
        or type(manifest["canvas_writes"]) is not int
        or manifest["canvas_writes"] != 0
    ):
        raise ValueError("Invalid attachment bundle state")
    limits = manifest["limits"]
    if (
        not isinstance(limits, dict)
        or set(limits) != {"max_file_bytes", "max_total_bytes"}
        or type(limits["max_file_bytes"]) is not int
        or not 1 <= limits["max_file_bytes"] <= MAX_FILE_BYTES
        or type(limits["max_total_bytes"]) is not int
        or not 1 <= limits["max_total_bytes"] <= MAX_ARCHIVE_BYTES
    ):
        raise ValueError("Invalid attachment bundle limits")
    selections = manifest["selections"]
    if not isinstance(selections, list) or not 1 <= len(selections) <= 20:
        raise ValueError("Invalid attachment bundle selections")
    identities = []
    for selection in selections:
        if not isinstance(selection, dict) or set(selection) != {
            "assignment_id",
            "user_id",
            "file_id",
            "expected_bytes",
        }:
            raise ValueError("Invalid attachment bundle reference")
        identities.append(
            tuple(
                numeric_id(selection[key])
                for key in ("assignment_id", "user_id", "file_id")
            )
        )
        size = selection["expected_bytes"]
        if size is not None and (
            type(size) is not int or not 0 <= size <= limits["max_file_bytes"]
        ):
            raise ValueError("Invalid expected attachment bytes")
    if (
        len(identities) != len(set(identities))
        or sum(selection["expected_bytes"] or 0 for selection in selections)
        > limits["max_total_bytes"]
    ):
        raise ValueError("Duplicate or oversized attachment bundle reference")
    items = manifest["items"]
    files = manifest["files"]
    if (
        not isinstance(items, list)
        or len(items) > len(selections)
        or not isinstance(files, dict)
    ):
        raise ValueError("Invalid attachment bundle index")
    referenced = set()
    total = 0
    for index, item in enumerate(items):
        if not isinstance(item, dict) or set(item) - {
            "assignment_id",
            "user_id",
            "file_id",
            "state",
            "artifact",
            "bytes",
            "sha256",
            "reason",
        }:
            raise ValueError("Invalid attachment outcome")
        if (
            tuple(
                numeric_id(item[key]) for key in ("assignment_id", "user_id", "file_id")
            )
            != identities[index]
        ):
            raise ValueError("Attachment reference mismatch")
        if item.get("state") not in {"saved", "rejected", "failed", "unattempted"}:
            raise ValueError("Invalid attachment outcome state")
        if item["state"] == "saved":
            if set(item) != {
                "assignment_id",
                "user_id",
                "file_id",
                "state",
                "artifact",
                "bytes",
                "sha256",
            }:
                raise ValueError("Invalid saved attachment fields")
            name = item["artifact"]
            if (
                not isinstance(name, str)
                or re.fullmatch(
                    r"attachment-"
                    + re.escape(identities[index][2])
                    + r"-[a-f0-9]{32}\.bin",
                    name,
                )
                is None
                or name in referenced
            ):
                raise ValueError("Invalid attachment artifact name")
            if (
                type(item["bytes"]) is not int
                or not 0 <= item["bytes"] <= limits["max_file_bytes"]
                or not isinstance(item["sha256"], str)
                or re.fullmatch(r"[a-f0-9]{64}", item["sha256"]) is None
            ):
                raise ValueError("Invalid attachment integrity metadata")
            expected = selections[index]["expected_bytes"]
            if expected is not None and expected != item["bytes"]:
                raise ValueError("Attachment expected byte mismatch")
            indexed = files.get(name)
            if (
                not isinstance(indexed, dict)
                or type(indexed.get("bytes")) is not int
                or indexed != {"bytes": item["bytes"], "sha256": item["sha256"]}
            ):
                raise ValueError("Attachment file index mismatch")
            referenced.add(name)
            total += item["bytes"]
        elif (
            set(item) != {"assignment_id", "user_id", "file_id", "state", "reason"}
            or not isinstance(item["reason"], str)
            or re.fullmatch(r"[a-z_]{1,100}", item["reason"]) is None
        ):
            raise ValueError("Invalid incomplete attachment fields")
    if (
        referenced != set(files)
        or type(manifest["total_bytes"]) is not int
        or total != manifest["total_bytes"]
        or total > limits["max_total_bytes"]
    ):
        raise ValueError("Attachment aggregate mismatch")
    if manifest["state"] == "complete" and (
        len(items) != len(selections) or any(item["state"] != "saved" for item in items)
    ):
        raise ValueError("False complete attachment bundle")
    return manifest


def _verify_attachment_bundle(store: ArtifactStore) -> dict[str, Any]:
    """Verify private attachment evidence and return no identities or member paths."""
    store.total_size()
    names = store.names()
    checkpoints = sorted(
        name for name in names if re.fullmatch(r"checkpoint-[0-9]{6}\.json", name)
    )
    if (
        not checkpoints
        or len(checkpoints) > 21
        or checkpoints
        != [f"checkpoint-{index + 1:06d}.json" for index in range(len(checkpoints))]
    ):
        raise ValueError("Missing attachment checkpoint")
    first = _validate_bundle_manifest(store.read_json(checkpoints[0]))
    immutable = {
        key: first[key]
        for key in first
        if key not in {"items", "files", "total_bytes", "observed_until", "state"}
    }
    latest = first
    for index, filename in enumerate(checkpoints):
        current = _validate_bundle_manifest(store.read_json(filename))
        if (
            current["state"] != "interrupted"
            or len(current["items"]) != index
            or {key: current[key] for key in immutable} != immutable
            or current["items"][: len(latest["items"])] != latest["items"]
            or any(
                current["files"].get(key) != value
                for key, value in latest["files"].items()
            )
        ):
            raise ValueError("Changed attachment checkpoint scope")
        latest = current
    finished = "manifest.json" in names
    manifest = (
        _validate_bundle_manifest(store.read_json("manifest.json"))
        if finished
        else latest
    )
    if finished and (
        manifest["state"] not in {"complete", "partial"}
        or len(manifest["items"]) != len(manifest["selections"])
        or {key: manifest[key] for key in immutable} != immutable
        or any(
            manifest[key] != latest[key]
            for key in ("files", "items", "total_bytes", "observed_until")
        )
    ):
        raise ValueError("Attachment completion marker mismatch")
    for filename, expected in manifest["files"].items():
        digest = hashlib.sha256()
        size = 0
        with os.fdopen(
            store.open_read(filename, manifest["limits"]["max_file_bytes"]), "rb"
        ) as stream:
            while chunk := stream.read(65536):
                size += len(chunk)
                digest.update(chunk)
        if size != expected["bytes"] or digest.hexdigest() != expected["sha256"]:
            raise ValueError("Attachment integrity mismatch")
    extras = (
        set(names)
        - set(checkpoints)
        - set(manifest["files"])
        - ({"manifest.json"} if finished else set())
    )
    if (
        finished
        and extras
        or any(
            re.fullmatch(
                r"(?:pending-[a-f0-9]{32}\.tmp|attachment-[0-9]+-[a-f0-9]{32}\.bin)",
                name,
            )
            is None
            for name in extras
        )
    ):
        raise ValueError("Unexpected attachment members")
    return {
        "schema_version": 1,
        "kind": "snapshot_attachment_download",
        "state": manifest["state"] if finished else "interrupted",
        "saved_files": len(manifest["files"]),
        "selected_files": len(manifest["selections"]),
        "total_bytes": manifest["total_bytes"],
        "parent_checkpoint_sha256": manifest["parent_checkpoint_sha256"],
        "integrity_verified": True,
        "incomplete_unindexed_files": len(extras),
        "canvas_writes": 0,
    }


def verify_attachment_bundle(store: ArtifactStore) -> dict[str, Any]:
    """Verify local attachment integrity with no identities, URLs or raw paths."""
    try:
        return _verify_attachment_bundle(store)
    except (ValueError, KeyError, TypeError, OSError, OverflowError):
        raise ValueError("Attachment bundle verification failed") from None


def register_snapshot_attachment_tools(mcp: FastMCP) -> None:
    @mcp.tool(
        annotations=ToolAnnotations(
            read_only_hint=False, destructive_hint=False, idempotent_hint=False
        )
    )
    async def download_snapshot_attachments(
        snapshot_directory: str,
        selected_files: list[dict[str, Any]],
        save_directory: str = "local_snapshots",
        max_file_bytes: int = 10 * 1024 * 1024,
        max_total_bytes: int = 20 * 1024 * 1024,
        confirmation_token: str | None = None,
    ) -> dict[str, Any]:
        """Preview then download up to twenty selected snapshot attachments privately.

        Local stdio only; educator grading permission and snapshot origin/course/
        caller are rechecked. The parent must include attachment metadata. Files
        are read using Canvas GETs and bounded HTTPS downloads, never Canvas writes.
        Known sizes and streamed bytes enforce per-file and total limits. Supported
        destinations are the configured public Canvas origin and public S3 hosts.
        Partial results preserve completed files with hashes; no automatic retries.
        Stored numeric references and file contents remain identifying local data.
        """
        if is_http_request_active():
            return {"error": "Attachment downloads are available only on local stdio."}
        if (
            type(max_file_bytes) is not int
            or not 1 <= max_file_bytes <= MAX_FILE_BYTES
            or type(max_total_bytes) is not int
            or not 1 <= max_total_bytes <= MAX_ARCHIVE_BYTES
        ):
            return {"error": "Invalid attachment byte limits."}
        directory: Path | None = None
        results: list[dict[str, Any]] = []
        try:
            destination = Path(save_directory).expanduser().absolute()
            source_directory = Path(snapshot_directory).expanduser().absolute()
            if ".." in destination.parts or ".." in source_directory.parts:
                raise ValueError("Unsafe local path")
            with ArtifactStore(source_directory) as source:
                source.lock(shared=True)
                parent = verify_snapshot(source)
                if parent["scope"].get("attachments") != "metadata_only":
                    raise ValueError("Parent snapshot excludes attachment metadata")
                selections = _selected_references(
                    source, parent, selected_files, max_file_bytes, max_total_bytes
                )
                parent_digest = hashlib.sha256(json_bytes(parent)).hexdigest()
            origin, course_id, caller_id = await _capture_context(parent["course_id"])
            if (origin, course_id, caller_id) != (
                parent["canvas_origin"],
                parent["course_id"],
                parent["caller_id"],
            ):
                raise ValueError("Snapshot context mismatch")
            preview = {
                "schema_version": 1,
                "canvas_origin": origin,
                "course_id": course_id,
                "snapshot_directory": str(source_directory),
                "parent_checkpoint_sha256": parent_digest,
                "selected_files": selections,
                "max_file_bytes": max_file_bytes,
                "max_total_bytes": max_total_bytes,
                "save_directory": str(destination),
                "notice": "Private local attachment contents and numeric IDs remain identifying. No Canvas writes. Snapshot and current submission observations are not atomic.",
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
            with ArtifactStore(source_directory) as source:
                source.lock(shared=True)
                if (
                    hashlib.sha256(json_bytes(verify_snapshot(source))).hexdigest()
                    != parent_digest
                ):
                    raise ValueError("Snapshot changed after preview")
            namespace = f"attachments-{parent_digest[:16]}-{uuid.uuid4().hex}"
            with ArtifactStore(destination, create=True) as root:
                os.mkdir(namespace, mode=0o700, dir_fd=root.fd)
                os.fsync(root.fd)
            directory = destination / namespace
            manifest: dict[str, Any] = {
                "schema_version": 1,
                "kind": "snapshot_attachment_download",
                "canvas_origin": origin,
                "course_id": course_id,
                "caller_id": caller_id,
                "parent_checkpoint_sha256": parent_digest,
                "started_at": now(),
                "state": "interrupted",
                "items": [],
                "files": {},
                "canvas_writes": 0,
                "selections": selections,
                "total_bytes": 0,
                "observed_until": now(),
                "limits": {
                    "max_file_bytes": max_file_bytes,
                    "max_total_bytes": max_total_bytes,
                },
            }
            total = 0
            with ArtifactStore(directory) as store:
                store.lock()
                store.write("checkpoint-000001.json", json_bytes(manifest))
                stop = False
                stop_reason = "stopped_after_download_failure"
                for index, selection in enumerate(selections):
                    item = {
                        key: selection[key]
                        for key in ("assignment_id", "user_id", "file_id")
                    }
                    item["state"] = "unattempted"
                    if not stop:
                        try:
                            response = await make_canvas_request(
                                "get",
                                canvas_path(
                                    "courses",
                                    course_id,
                                    "assignments",
                                    selection["assignment_id"],
                                    "submissions",
                                    selection["user_id"],
                                ),
                                skip_anonymization=True,
                                _max_response_bytes=MAX_FILE_BYTES,
                            )
                            if not isinstance(response, dict) or "error" in response:
                                stop = getattr(response, "status_code", None) in {
                                    401,
                                    403,
                                }
                                if stop:
                                    stop_reason = "stopped_after_authorization_failure"
                                raise DownloadRejected("submission_unavailable")
                            if (
                                numeric_id(response.get("assignment_id"))
                                != selection["assignment_id"]
                                or numeric_id(response.get("user_id"))
                                != selection["user_id"]
                            ):
                                raise DownloadRejected("submission_identity_changed")
                            attachments = response.get("attachments")
                            if not isinstance(attachments, list):
                                raise DownloadRejected("attachment_unavailable")
                            matching = [
                                value
                                for value in attachments
                                if isinstance(value, dict)
                                and numeric_id(value.get("id")) == selection["file_id"]
                            ]
                            if len(matching) != 1:
                                raise DownloadRejected("attachment_unavailable")
                            attachment = matching[0]
                            fresh_size = attachment.get("size")
                            if fresh_size is not None and (
                                type(fresh_size) is not int
                                or fresh_size < 0
                                or fresh_size > max_file_bytes
                                or fresh_size > max_total_bytes - total
                                or selection["expected_bytes"] is not None
                                and fresh_size != selection["expected_bytes"]
                            ):
                                raise DownloadRejected(
                                    "attachment_size_changed_or_limited"
                                )
                            name = f"attachment-{selection['file_id']}-{uuid.uuid4().hex}.bin"
                            metadata = await asyncio.wait_for(
                                _download_member(
                                    store,
                                    name,
                                    attachment.get("url"),
                                    origin,
                                    min(
                                        max_file_bytes,
                                        max_total_bytes - total,
                                        MAX_ARCHIVE_BYTES
                                        - store.total_size()
                                        - 2 * MAX_MANIFEST_BYTES,
                                    ),
                                    selection["expected_bytes"]
                                    if selection["expected_bytes"] is not None
                                    else fresh_size,
                                ),
                                timeout=float(get_config().api_timeout) * 4,
                            )
                            total += metadata["bytes"]
                            manifest["files"][name] = metadata
                            item.update(state="saved", artifact=name, **metadata)
                        except DownloadRejected as failure:
                            stop = True
                            item.update(state="rejected", reason=str(failure))
                        except Exception:
                            stop = True
                            item.update(
                                state="failed", reason="download_or_storage_unavailable"
                            )
                    else:
                        item["reason"] = stop_reason
                    manifest["items"].append(item)
                    results.append(item)
                    manifest.update(observed_until=now(), total_bytes=total)
                    store.write(
                        f"checkpoint-{index + 2:06d}.json", json_bytes(manifest)
                    )
                manifest["state"] = (
                    "complete"
                    if all(item["state"] == "saved" for item in results)
                    else "partial"
                )
                store.write("manifest.json", json_bytes(manifest))
            summary = {
                "schema_version": 1,
                "state": manifest["state"],
                "attachment_directory": str(directory),
                "saved_files": sum(item["state"] == "saved" for item in results),
                "selected_files": len(selections),
                "total_bytes": total,
                "parent_checkpoint_sha256": parent_digest,
                "canvas_writes": 0,
                "next_step": "Inspect the private manifest and hashes; incomplete selections require a fresh preview before any new download.",
            }
            if manifest["state"] != "complete":
                summary["error"] = (
                    "Some attachments were not downloaded; completed files are retained privately."
                )
            return summary
        except Exception:
            result: dict[str, Any] = {
                "error": "Attachment download could not complete. Verify snapshot scope, educator permission, byte limits and private storage. No raw records or URLs are included.",
                "canvas_writes": 0,
            }
            if directory is not None:
                result.update(
                    state="partial",
                    attachment_directory=str(directory),
                    saved_files=sum(item["state"] == "saved" for item in results),
                )
            return result
