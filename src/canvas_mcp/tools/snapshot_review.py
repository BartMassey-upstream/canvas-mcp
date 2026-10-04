"""Local integrity review, count comparisons and selective identity lookup."""

from __future__ import annotations

import csv
import hashlib
import io
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from fastmcp import FastMCP
from mcp.types import ToolAnnotations

from ..core.artifact_store import (
    MAX_ARCHIVE_BYTES,
    MAX_FILE_BYTES,
    ArtifactStore,
    canonical_origin,
    json_bytes,
)
from ..core.config import get_config
from ..core.credentials import is_http_request_active
from ..core.csv_safety import csv_safe_cell
from ..core.local_artifacts import write_private_bundle
from ..core.record_snapshots import (
    FAMILIES,
    numeric_id,
    record_key,
    snapshot_summary,
    verified_records,
    verify_snapshot,
)
from ..core.validation import validate_params

_MISSING_FIELDS = {
    "attempt",
    "workflow_state",
    "score",
    "submitted_at",
    "late",
    "missing",
    "points_possible",
    "grading_type",
    "attachments",
}
_ALGORITHM = "sha256-id8-student-v1"


def _now() -> datetime:
    return datetime.now(UTC)


def _rejected(message: str) -> dict[str, Any]:
    return {"status": "rejected", "error": message, "canvas_writes": 0}


def _safe_summary(manifest: dict[str, Any]) -> dict[str, Any]:
    if type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1:
        raise ValueError("Invalid schema")
    raw = snapshot_summary(manifest)
    families = {}
    for family in manifest["scope"]["families"]:
        item = raw["families"].get(
            family, {"records": 0, "states": {}, "unavailable_fields": {}}
        )
        if (
            family not in FAMILIES
            or type(item["records"]) is not int
            or not 0 <= item["records"] <= MAX_ARCHIVE_BYTES
        ):
            raise ValueError("Invalid family counts")
        states = item["states"]
        missing = item["unavailable_fields"]
        if (
            not set(states)
            <= {"pending", "collecting", "complete", "unavailable", "failed"}
            or not set(missing) <= _MISSING_FIELDS
        ):
            raise ValueError("Invalid summary fields")
        if any(
            type(count) is not int or count < 0 or count > MAX_ARCHIVE_BYTES
            for count in [*states.values(), *missing.values()]
        ):
            raise ValueError("Invalid summary counts")
        families[family] = {
            "records": item["records"],
            "states": states,
            "unavailable_fields": missing,
            "coverage_complete": _coverage(manifest, family)[0],
            "unavailable_units": _coverage(manifest, family)[1],
        }
    return {
        "schema_version": 1,
        "state": raw["state"],
        "families": families,
        "started_at": datetime.fromisoformat(raw["started_at"])
        .astimezone(UTC)
        .isoformat(),
        "observed_until": datetime.fromisoformat(raw["observed_until"])
        .astimezone(UTC)
        .isoformat(),
        "changes_detected": bool(manifest["consistency_issues"]),
        "resume_possible": bool(raw["resume_possible"]),
        "recapture_required": bool(raw["recapture_required"]),
        "canvas_writes": 0,
        "consistency": "Scoped nontransactional observations; a verified archive is not whole-course proof.",
    }


def _family_records(
    store: ArtifactStore, manifest: dict[str, Any], family: str
) -> dict[str, str]:
    records = {}
    for unit in manifest["units"].values():
        if unit["family"] == family:
            for filename in unit["files"]:
                for row in verified_records(
                    store, filename, manifest["files"][filename]
                ):
                    records[record_key(family, row)] = hashlib.sha256(
                        json_bytes(row)
                    ).hexdigest()
    return records


def _coverage(manifest: dict[str, Any], family: str) -> tuple[bool, int]:
    units = {
        key: unit for key, unit in manifest["units"].items() if unit["family"] == family
    }
    incomplete = sum(
        unit["state"] != "complete" or key in manifest["consistency_issues"]
        for key, unit in units.items()
    )
    if family in {"submissions", "peer_reviews"} and (
        manifest["units"].get("assignments", {}).get("state") != "complete"
        or "assignments" in manifest["consistency_issues"]
    ):
        incomplete += 1
    return incomplete == 0, incomplete


def _identity_records(
    store: ArtifactStore, course_id: str, max_age_days: int
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    manifest = store.read_json("manifest.json")
    if (
        not isinstance(manifest, dict)
        or type(manifest.get("schema_version")) is not int
        or manifest["schema_version"] != 1
    ):
        raise ValueError("Invalid identity schema")
    if (
        manifest.get("kind") != "student_identity_map"
        or manifest.get("state") != "complete"
        or manifest.get("pseudonym_algorithm") != _ALGORITHM
    ):
        raise ValueError("Invalid identity map")
    if (
        manifest.get("canvas_origin") != canonical_origin(get_config().canvas_api_url)
        or manifest.get("course_id") != course_id
    ):
        raise ValueError("Wrong identity scope")
    observed = datetime.fromisoformat(manifest["created_at"])
    current = _now()
    if (
        observed.tzinfo is None
        or observed > current
        or current - observed > timedelta(days=max_age_days)
    ):
        raise ValueError("Identity map is not current")
    if set(manifest.get("files", {})) != {"identities.json", "anonymization_map.csv"}:
        raise ValueError("Incomplete identity files")
    if set(store.names()) != {
        "manifest.json",
        "identities.json",
        "anonymization_map.csv",
    }:
        raise ValueError("Unexpected identity files")
    payloads = {}
    total = 0
    for name, metadata in manifest["files"].items():
        if (
            not isinstance(metadata, dict)
            or type(metadata.get("bytes")) is not int
            or not 0 <= metadata["bytes"] <= MAX_FILE_BYTES
            or not isinstance(metadata.get("sha256"), str)
            or not re.fullmatch(r"[a-f0-9]{64}", metadata["sha256"])
        ):
            raise ValueError("Invalid file metadata")
        data = store.read(name)
        total += len(data)
        if (
            total > MAX_ARCHIVE_BYTES
            or len(data) != metadata["bytes"]
            or hashlib.sha256(data).hexdigest() != metadata["sha256"]
        ):
            raise ValueError("Identity integrity mismatch")
        payloads[name] = data
    records = store.decode_json(payloads["identities.json"])
    if (
        not isinstance(records, list)
        or type(manifest.get("record_count")) is not int
        or not 0 < len(records) <= 100000
        or len(records) != manifest["record_count"]
    ):
        raise ValueError("Invalid identity count")
    seen_ids: set[int] = set()
    seen_pseudonyms: set[str] = set()
    for record in records:
        if not isinstance(record, dict) or set(record) != {
            "real_id",
            "real_name",
            "real_email",
            "anonymous_id",
        }:
            raise ValueError("Invalid identity record")
        real_id = record["real_id"]
        if type(real_id) is not int or real_id <= 0 or real_id in seen_ids:
            raise ValueError("Invalid identity ID")
        expected = "Student_" + hashlib.sha256(str(real_id).encode()).hexdigest()[:8]
        if record["anonymous_id"] != expected or expected in seen_pseudonyms:
            raise ValueError("Invalid pseudonym")
        if any(
            record[field] is not None and not isinstance(record[field], str)
            for field in ("real_name", "real_email")
        ):
            raise ValueError("Invalid identity value")
        seen_ids.add(real_id)
        seen_pseudonyms.add(expected)
    return manifest, records


def _archive_kind(store: ArtifactStore) -> str:
    names = store.names()
    if "manifest.json" in names:
        marker = "manifest.json"
    else:
        checkpoints = sorted(
            name for name in names if re.fullmatch(r"checkpoint-[0-9]{6}\.json", name)
        )
        if not checkpoints:
            raise ValueError("Missing archive metadata")
        marker = checkpoints[-1]
    document = store.read_json(marker)
    if not isinstance(document, dict):
        raise ValueError("Unsupported archive kind")
    kind = document.get("kind")
    if not isinstance(kind, str) or kind not in {
        "student_record_snapshot",
        "snapshot_attachment_download",
    }:
        raise ValueError("Unsupported archive kind")
    return kind


def register_snapshot_review_tools(mcp: FastMCP) -> None:
    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    @validate_params
    async def verify_record_snapshot(snapshot_directory: str) -> dict[str, Any]:
        """Verify a private local record or attachment archive and return safe counts.

        Args:
            snapshot_directory: Existing private snapshot directory, local stdio only.
        """
        if is_http_request_active():
            return _rejected("Snapshot verification is available only on local stdio.")
        try:
            with ArtifactStore(snapshot_directory) as store:
                store.lock(shared=True)
                if _archive_kind(store) == "snapshot_attachment_download":
                    from .snapshot_attachments import verify_attachment_bundle

                    summary = verify_attachment_bundle(store)
                else:
                    summary = _safe_summary(verify_snapshot(store))
            return {"status": "verified", **summary}
        except Exception:
            return _rejected(
                "Snapshot verification failed; check private files, schema, scope and integrity."
            )

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
    @validate_params
    async def compare_record_snapshots(
        before_directory: str, after_directory: str
    ) -> dict[str, Any]:
        """Compare verified matching snapshots; return observation counts only.

        Added/removed counts are unknown for incompletely observed families.
        Changed counts compare records observed in both archives, not an atomic
        Canvas transaction. Only record archives are compared; attachment
        download bundles can be verified separately. No Canvas request or
        local write is made.

        Args:
            before_directory: Earlier existing private snapshot directory.
            after_directory: Later existing private snapshot directory.
        """
        if is_http_request_active():
            return _rejected("Snapshot comparison is available only on local stdio.")
        try:
            with (
                ArtifactStore(before_directory) as before,
                ArtifactStore(after_directory) as after,
            ):
                before.lock(shared=True)
                after.lock(shared=True)
                left, right = verify_snapshot(before), verify_snapshot(after)
                _safe_summary(left)
                _safe_summary(right)
                if any(
                    left[key] != right[key]
                    for key in (
                        "canvas_origin",
                        "course_id",
                        "caller_id",
                        "schema_version",
                        "scope",
                    )
                ):
                    return _rejected(
                        "Snapshots have different origin, course, caller, schema or coverage scope."
                    )
                families = {}
                for family in left["scope"]["families"]:
                    old, new = (
                        _family_records(before, left, family),
                        _family_records(after, right, family),
                    )
                    old_complete, old_unavailable = _coverage(left, family)
                    new_complete, new_unavailable = _coverage(right, family)
                    complete = old_complete and new_complete
                    families[family] = {
                        "before_records": len(old),
                        "after_records": len(new),
                        "added": len(new.keys() - old.keys()) if complete else None,
                        "removed": len(old.keys() - new.keys()) if complete else None,
                        "changed": sum(
                            old[key] != new[key] for key in old.keys() & new.keys()
                        ),
                        "unavailable": {
                            "before_units": old_unavailable,
                            "after_units": new_unavailable,
                        },
                        "coverage_complete": complete,
                    }
            return {
                "status": "compared",
                "families": families,
                "canvas_writes": 0,
                "consistency": "Counts compare scoped observations; null additions/removals mean incomplete coverage.",
            }
        except Exception:
            return _rejected(
                "Snapshot comparison failed; verify private files and matching archive integrity."
            )

    @mcp.tool(
        annotations=ToolAnnotations(destructive_hint=False, idempotent_hint=False)
    )
    @validate_params
    async def lookup_student_identities(
        map_directory: str,
        anonymous_ids: list[str],
        course_identifier: str | int,
        save_directory: str = "local_maps",
        max_age_days: int = 7,
    ) -> dict[str, Any]:
        """Save at most fifty selected identities privately without returning them.

        This local write makes no Canvas calls. Existing map origin, numeric
        course ID, declared algorithm, age, private files and integrity must
        validate. Returns only selected count and newly created bundle path.

        Args:
            map_directory: Existing private identity-map bundle directory.
            anonymous_ids: One to fifty unique Student_<eight lowercase hex> IDs.
            course_identifier: Numeric Canvas course ID; course codes/SIS IDs are refused.
            save_directory: Private local destination for a new selection bundle.
            max_age_days: Maximum accepted map age, one to 365 days (default seven).
        """
        if is_http_request_active():
            return _rejected("Identity lookup is available only on local stdio.")
        try:
            if (
                not 1 <= len(anonymous_ids) <= 50
                or len(set(anonymous_ids)) != len(anonymous_ids)
                or any(
                    not re.fullmatch(r"Student_[a-f0-9]{8}", value)
                    for value in anonymous_ids
                )
                or type(max_age_days) is not int
                or not 1 <= max_age_days <= 365
            ):
                return _rejected(
                    "Select one to fifty unique pseudonyms and a map age from one to 365 days."
                )
            course_id = numeric_id(course_identifier)
            with ArtifactStore(map_directory) as store:
                store.lock(shared=True)
                manifest, records = _identity_records(store, course_id, max_age_days)
            selected_ids = set(anonymous_ids)
            selected = [
                record for record in records if record["anonymous_id"] in selected_ids
            ]
            if len(selected) != len(selected_ids):
                return _rejected(
                    "Some selected pseudonyms are absent from the verified map; no selection was written."
                )
            output = io.StringIO(newline="")
            writer = csv.DictWriter(
                output,
                fieldnames=["real_name", "real_id", "real_email", "anonymous_id"],
            )
            writer.writeheader()
            writer.writerows(
                {
                    **row,
                    "real_name": csv_safe_cell(row["real_name"] or ""),
                    "real_email": csv_safe_cell(row["real_email"] or ""),
                }
                for row in selected
            )
            origin = manifest["canvas_origin"]
            metadata = {
                "schema_version": 1,
                "kind": "student_identity_selection",
                "canvas_origin": origin,
                "course_id": course_id,
                "pseudonym_algorithm": _ALGORITHM,
                "created_at": _now().isoformat(),
                "source_created_at": manifest["created_at"],
                "record_count": len(selected),
            }
        except Exception:
            return _rejected(
                "Identity-map verification failed; check origin, numeric course, age, private files and integrity."
            )
        try:
            origin_hash = hashlib.sha256(origin.encode()).hexdigest()[:16]
            bundle = write_private_bundle(
                save_directory,
                f"canvas-{origin_hash}-course-{course_id}-selection-v1",
                {
                    "identities.json": json_bytes(selected),
                    "anonymization_map.csv": output.getvalue().encode(),
                },
                metadata,
            )
            return {
                "status": "saved",
                "bundle_path": str(bundle),
                "selected_count": len(selected),
                "canvas_writes": 0,
            }
        except Exception:
            return {
                "status": "unconfirmed",
                "error": "Local selection completion could not be confirmed; inspect the private destination.",
                "canvas_writes": 0,
            }
