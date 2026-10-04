"""Versioned, resumable snapshots of explicitly scoped Canvas read evidence."""

from __future__ import annotations

import hashlib
import html
import re
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .. import __version__
from .artifact_store import (
    MAX_ARCHIVE_BYTES,
    MAX_FILE_BYTES,
    MAX_MANIFEST_BYTES,
    MAX_RECORD_BYTES,
    ArtifactStore,
    canonical_origin,
    json_bytes,
)
from .client import make_canvas_request
from .config import get_config
from .path import canvas_path

FAMILIES = {
    "course",
    "assignments",
    "enrollments",
    "sections",
    "groups",
    "submissions",
    "peer_reviews",
    "outcomes",
}
CONSISTENCY = "Nontransactional reads of records visible to this caller; completeness is scoped, not whole-course proof."
DEFAULT_FAMILIES = [
    "course",
    "assignments",
    "submissions",
    "enrollments",
    "sections",
    "groups",
]
_FIELDS = {
    "course": (
        "id",
        "workflow_state",
        "start_at",
        "end_at",
        "enrollment_term_id",
        "updated_at",
    ),
    "assignments": (
        "id",
        "course_id",
        "assignment_group_id",
        "points_possible",
        "grading_type",
        "submission_types",
        "published",
        "due_at",
        "lock_at",
        "unlock_at",
        "updated_at",
        "use_rubric_for_grading",
        "rubric_settings",
    ),
    "enrollments": (
        "id",
        "course_id",
        "course_section_id",
        "user_id",
        "type",
        "role_id",
        "enrollment_state",
        "created_at",
        "updated_at",
    ),
    "sections": ("id", "course_id", "start_at", "end_at", "nonxlist_course_id"),
    "groups": ("id", "course_id", "group_category_id", "members_count"),
    "submissions": (
        "id",
        "assignment_id",
        "user_id",
        "attempt",
        "submission_type",
        "workflow_state",
        "submitted_at",
        "graded_at",
        "score",
        "grade",
        "late",
        "missing",
        "excused",
        "late_policy_status",
        "seconds_late",
        "points_deducted",
        "posted_at",
        "grade_matches_current_submission",
        "updated_at",
    ),
    "peer_reviews": (
        "id",
        "asset_id",
        "asset_type",
        "user_id",
        "assessor_id",
        "workflow_state",
    ),
    "outcomes": (
        "id",
        "score",
        "mastery",
        "possible",
        "percent",
        "submitted_or_assessed_at",
        "links",
    ),
}
_MISSING = {
    "submissions": (
        "attempt",
        "workflow_state",
        "score",
        "submitted_at",
        "late",
        "missing",
    ),
    "assignments": ("points_possible", "grading_type"),
}


def now() -> str:
    return datetime.now(UTC).isoformat()


def numeric_id(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, str | int):
        raise ValueError("Expected numeric scoped ID")
    text = str(value)
    if not text.isascii() or not text.isdecimal() or int(text) < 1:
        raise ValueError("Expected positive scoped ID")
    return str(int(text))


def normalized_scope(
    families: list[str] | None,
    assignment_ids: list[int] | None,
    include_bodies: bool,
    include_comments: bool,
    include_identities: bool,
    include_attachment_metadata: bool = False,
) -> dict[str, Any]:
    selected = set(families if families is not None else DEFAULT_FAMILIES)
    if not selected or not selected <= FAMILIES:
        raise ValueError("Unsupported snapshot families")
    selected.add("course")
    if selected & {"submissions", "peer_reviews"}:
        selected.add("assignments")
    ids = sorted({numeric_id(i) for i in assignment_ids or []}, key=int)
    if len(ids) > 1000:
        raise ValueError("At most 1000 selected assignments")
    return {
        "families": sorted(selected),
        "assignment_ids": ids,
        "include_bodies": include_bodies,
        "include_comments": include_comments,
        "include_identities": include_identities,
        "enrollment_states": ["active", "invited", "completed", "inactive"],
        "attachments": "metadata_only" if include_attachment_metadata else "excluded",
        "inbox_discussions": "excluded",
    }


def new_manifest(
    origin: str, course_id: str, caller_id: str, scope: dict[str, Any]
) -> dict[str, Any]:
    units = {
        f: {
            "family": f,
            "state": "pending",
            "page": "1",
            "files": [],
            "count": 0,
            "missing_fields": {},
            "seen_pages": [],
        }
        for f in scope["families"]
        if f not in {"submissions", "peer_reviews"}
    }
    return {
        "schema_version": 1,
        "kind": "student_record_snapshot",
        "package_version": __version__,
        "canvas_origin": origin,
        "course_id": course_id,
        "caller_id": caller_id,
        "scope": scope,
        "started_at": now(),
        "observed_until": now(),
        "generation": 0,
        "state": "interrupted",
        "units": units,
        "files": {},
        "consistency_issues": [],
        "consistency": CONSISTENCY,
    }


def checkpoint(store: ArtifactStore, manifest: dict[str, Any]) -> None:
    manifest["observed_until"] = now()
    if (
        manifest["generation"] >= 9999
        or len(manifest["files"]) > 10000
        or len(manifest["units"]) > 3000
    ):
        raise ValueError("Snapshot checkpoint limit; start a narrower capture")
    manifest["generation"] += 1
    data = json_bytes(manifest)
    if (
        len(data) > MAX_MANIFEST_BYTES
        or store.total_size() + len(data) > MAX_ARCHIVE_BYTES
    ):
        raise ValueError("Snapshot manifest limit exceeded")
    store.write(f"checkpoint-{manifest['generation']:06d}.json", data)


def _read_manifest(store: ArtifactStore) -> dict[str, Any]:
    candidates = sorted(
        n for n in store.names() if re.fullmatch(r"checkpoint-[0-9]{6}\.json", n)
    )
    if not candidates:
        raise ValueError("No snapshot checkpoint")
    manifest = store.read_json(candidates[-1])
    if (
        not isinstance(manifest, dict)
        or type(manifest.get("schema_version")) is not int
        or manifest.get("schema_version") != 1
        or manifest.get("kind") != "student_record_snapshot"
    ):
        raise ValueError("Unsupported snapshot schema")
    if (
        type(manifest.get("generation")) is not int
        or not 1 <= manifest["generation"] <= 10000
    ):
        raise ValueError("Invalid checkpoint generation")
    if candidates != [
        f"checkpoint-{i:06d}.json" for i in range(1, manifest.get("generation", 0) + 1)
    ]:
        raise ValueError("Missing checkpoint generation")
    if manifest.get("state") not in {"complete", "partial", "interrupted"}:
        raise ValueError("Invalid snapshot state")
    for key in ("started_at", "observed_until"):
        if datetime.fromisoformat(manifest[key]).tzinfo is None:
            raise ValueError("Missing observation timezone")
    if datetime.fromisoformat(manifest["observed_until"]) < datetime.fromisoformat(
        manifest["started_at"]
    ):
        raise ValueError("Observation window is reversed")
    if (
        numeric_id(manifest["course_id"]) != manifest["course_id"]
        or numeric_id(manifest["caller_id"]) != manifest["caller_id"]
    ):
        raise ValueError("Noncanonical scope IDs")
    if canonical_origin(manifest["canvas_origin"]) != manifest["canvas_origin"]:
        raise ValueError("Invalid snapshot origin")
    if manifest.get("consistency") != CONSISTENCY or not isinstance(
        manifest.get("consistency_issues"), list
    ):
        raise ValueError("Invalid consistency metadata")
    scope = manifest["scope"]
    if scope != normalized_scope(
        scope["families"],
        scope["assignment_ids"],
        scope["include_bodies"],
        scope["include_comments"],
        scope["include_identities"],
        scope.get("attachments") == "metadata_only",
    ):
        raise ValueError("Invalid snapshot scope")
    if any(
        type(scope[k]) is not bool
        for k in ("include_bodies", "include_comments", "include_identities")
    ):
        raise ValueError("Invalid inclusion flags")
    if not isinstance(manifest.get("files"), dict) or not isinstance(
        manifest.get("units"), dict
    ):
        raise ValueError("Invalid snapshot index")
    return manifest


def record_key(family: str, row: dict[str, Any]) -> str:
    if family == "submissions":
        return numeric_id(row["assignment_id"]) + ":" + numeric_id(row["user_id"])
    if family == "peer_reviews":
        return ":".join(
            numeric_id(row[k]) for k in ("asset_id", "user_id", "assessor_id")
        )
    return numeric_id(row["id"])


def verified_records(
    store: ArtifactStore, name: str, expected: dict[str, Any]
) -> Iterator[dict[str, Any]]:
    data = store.read(name)
    if (
        len(data) != expected["bytes"]
        or hashlib.sha256(data).hexdigest() != expected["sha256"]
    ):
        raise ValueError("Record file changed after verification")
    yield from store.decode_records(data)


def verify_snapshot(store: ArtifactStore) -> dict[str, Any]:
    store.total_size()
    manifest = _read_manifest(store)
    if len(manifest["files"]) > 10000 or len(manifest["units"]) > 3000:
        raise ValueError("Snapshot index limit exceeded")
    if any(
        not isinstance(k, str) or k not in manifest["units"]
        for k in manifest["consistency_issues"]
    ):
        raise ValueError("Invalid consistency unit")
    total = 0
    referenced: set[str] = set()
    seen_global: dict[str, set[str]] = {}
    for key, unit in manifest["units"].items():
        if not isinstance(unit, dict):
            raise ValueError("Invalid unit object")
        family = unit.get("family")
        if not isinstance(family, str) or family not in manifest["scope"]["families"]:
            raise ValueError("Unexpected snapshot family")
        expected_key = family
        if family in {"submissions", "peer_reviews"}:
            expected_key += "-" + numeric_id(unit.get("assignment_id"))
        if key != expected_key or unit.get("state") not in {
            "pending",
            "collecting",
            "complete",
            "unavailable",
            "failed",
        }:
            raise ValueError("Invalid snapshot unit")
        if type(unit.get("count")) is not int or not 0 <= unit["count"] <= 100000:
            raise ValueError("Invalid record count")
        allowed_missing = set(_MISSING.get(family, ()))
        if (
            family == "submissions"
            and manifest["scope"]["attachments"] == "metadata_only"
        ):
            allowed_missing.add("attachments")
        if not isinstance(unit.get("missing_fields"), dict) or any(
            k not in allowed_missing
            or type(v) is not int
            or not 0 <= v <= unit["count"]
            for k, v in unit["missing_fields"].items()
        ):
            raise ValueError("Invalid missing-field counts")
        if not isinstance(unit.get("files"), list) or len(unit["files"]) > 10000:
            raise ValueError("Invalid file index")
        if (
            not isinstance(unit.get("seen_pages"), list)
            or len(unit["seen_pages"]) > 10000
            or any(
                not isinstance(page, str)
                or re.fullmatch(r"[A-Za-z0-9_.:=-]{1,256}", page) is None
                for page in unit["seen_pages"]
            )
        ):
            raise ValueError("Invalid page history")
        if (
            not isinstance(unit.get("page"), str)
            or re.fullmatch(r"[A-Za-z0-9_.:=-]{1,256}", unit["page"]) is None
        ):
            raise ValueError("Invalid page cursor")
        if unit.get("error") not in {
            None,
            "read_unavailable",
            "cursor_expired",
            "scope_limit",
            "selected_assignments_unavailable",
            "invalid_or_oversized_response",
        }:
            raise ValueError("Invalid recovery state")
        seen = seen_global.setdefault(family, set())
        count = 0
        missing_counts: dict[str, int] = {}
        for name in unit["files"]:
            if name in referenced or not re.fullmatch(
                r"records-[a-f0-9]{32}\.jsonl", name
            ):
                raise ValueError("Invalid or duplicate record file")
            referenced.add(name)
            expected = manifest["files"][name]
            if (
                not isinstance(expected, dict)
                or type(expected.get("bytes")) is not int
                or expected["bytes"] < 0
                or not isinstance(expected.get("sha256"), str)
                or re.fullmatch(r"[a-f0-9]{64}", expected["sha256"]) is None
            ):
                raise ValueError("Invalid file integrity metadata")
            data = store.read(name)
            total += len(data)
            if (
                total > MAX_ARCHIVE_BYTES
                or len(data) != expected["bytes"]
                or hashlib.sha256(data).hexdigest() != expected["sha256"]
            ):
                raise ValueError("Snapshot integrity mismatch")
            for row in store.decode_records(data):
                _validate_record(row, family, manifest["scope"])
                for field in row["unavailable_fields"]:
                    missing_counts[field] = missing_counts.get(field, 0) + 1
                identity = record_key(family, row)
                if identity in seen:
                    raise ValueError("Duplicate snapshot record")
                seen.add(identity)
                if (
                    family == "submissions"
                    and numeric_id(row["assignment_id"]) != unit["assignment_id"]
                ):
                    raise ValueError("Wrong assignment in snapshot unit")
                if (
                    family == "course"
                    and numeric_id(row["id"]) != manifest["course_id"]
                ):
                    raise ValueError("Wrong course in snapshot")
                if (
                    "course_id" in row
                    and numeric_id(row["course_id"]) != manifest["course_id"]
                ):
                    raise ValueError("Cross-course snapshot record")
                count += 1
        if missing_counts != unit["missing_fields"]:
            raise ValueError("Unavailable-field count mismatch")
        if family == "course" and unit["state"] == "complete" and count != 1:
            raise ValueError("Complete course unit requires identity evidence")
        if count != unit["count"]:
            raise ValueError("Snapshot count mismatch")
    if referenced != set(manifest["files"]):
        raise ValueError("Unreferenced indexed snapshot file")
    _validate_units(manifest, store)
    if manifest["state"] == "complete" and (
        any(u["state"] != "complete" for u in manifest["units"].values())
        or manifest["consistency_issues"]
    ):
        raise ValueError("False complete snapshot")
    return manifest


def _assignment_ids(manifest: dict[str, Any], store: ArtifactStore) -> list[str]:
    result: list[str] = []
    for name in manifest["units"].get("assignments", {}).get("files", []):
        result.extend(
            numeric_id(row["id"])
            for row in verified_records(store, name, manifest["files"][name])
        )
    return sorted(result, key=int)


def _validate_units(manifest: dict[str, Any], store: ArtifactStore) -> None:
    expected = set(manifest["scope"]["families"]) - {"submissions", "peer_reviews"}
    if not expected <= set(manifest["units"]):
        raise ValueError("Missing base snapshot unit")
    assignments = manifest["units"].get("assignments", {})
    if assignments.get("state") == "complete":
        for family in {"submissions", "peer_reviews"} & set(
            manifest["scope"]["families"]
        ):
            expected.update(f"{family}-{i}" for i in _assignment_ids(manifest, store))
    if set(manifest["units"]) != expected:
        raise ValueError("Missing or unexpected dependent units")
    selected = set(manifest["scope"]["assignment_ids"])
    if (
        selected
        and assignments.get("state") == "complete"
        and set(_assignment_ids(manifest, store)) != selected
    ):
        raise ValueError("Missing selected assignment evidence")
    if selected and not set(_assignment_ids(manifest, store)) <= selected:
        raise ValueError("Assignment filter mismatch")


def _validate_record(row: dict[str, Any], family: str, scope: dict[str, Any]) -> None:
    extras = {
        "course": {"syllabus_body"},
        "assignments": {"name", "description", "rubric"},
        "submissions": {
            "body",
            "submission_comments",
            "rubric_assessment",
            "attachments",
        },
        "enrollments": {"identity"},
    }
    if (
        set(row)
        - set(_FIELDS[family])
        - extras.get(family, set())
        - {"withheld_fields", "unavailable_fields"}
    ):
        raise ValueError("Unknown snapshot fields")
    if not scope["include_bodies"] and set(row) & {
        "body",
        "name",
        "description",
        "syllabus_body",
    }:
        raise ValueError("Content outside declared scope")
    if not scope["include_identities"] and "identity" in row:
        raise ValueError("Identities outside declared scope")
    if not scope["include_comments"] and (
        "submission_comments" in row
        or any(
            "comments" in criterion
            for criterion in row.get("rubric_assessment", {}).values()
        )
    ):
        raise ValueError("Comments outside declared scope")
    if "attachments" in row:
        if (
            scope["attachments"] != "metadata_only"
            or not isinstance(row["attachments"], list)
            or len(row["attachments"]) > 100
        ):
            raise ValueError("Attachment references outside declared scope")
        for attachment in row["attachments"]:
            if not isinstance(attachment, dict) or set(attachment) - {
                "id",
                "size",
                "content-type",
            }:
                raise ValueError("Invalid attachment reference")
            numeric_id(attachment["id"])
            if "size" in attachment and (
                type(attachment["size"]) is not int or attachment["size"] < 0
            ):
                raise ValueError("Invalid attachment size")
    record_key(family, row)
    if family in {"assignments", "enrollments", "sections", "groups"}:
        numeric_id(row.get("course_id"))
    if family == "enrollments":
        numeric_id(row.get("user_id"))
        if "type" in row and row["type"] != "StudentEnrollment":
            raise ValueError("Enrollment type outside declared scope")
        if (
            "enrollment_state" in row
            and row["enrollment_state"] not in scope["enrollment_states"]
        ):
            raise ValueError("Enrollment state outside declared scope")
    for key, value in row.items():
        if key.endswith("_id") and value is not None:
            numeric_id(value)
        if (
            key
            in {
                "late",
                "missing",
                "excused",
                "published",
                "use_rubric_for_grading",
                "grade_matches_current_submission",
            }
            and value is not None
            and type(value) is not bool
        ):
            raise ValueError("Invalid boolean record field")
        if (
            key
            in {
                "points_possible",
                "score",
                "points_deducted",
                "seconds_late",
                "attempt",
                "members_count",
                "possible",
                "percent",
            }
            and value is not None
            and type(value) not in {int, float}
        ):
            raise ValueError("Invalid numeric record field")
        if (
            key
            in {
                "body",
                "description",
                "name",
                "syllabus_body",
                "grade",
                "workflow_state",
                "submission_type",
                "grading_type",
                "late_policy_status",
                "enrollment_state",
                "type",
                "asset_type",
            }
            and value is not None
            and not isinstance(value, str)
        ):
            raise ValueError("Invalid text record field")
    missing = row.get("unavailable_fields")
    allowed_missing = set(_MISSING.get(family, ()))
    if family == "submissions" and scope["attachments"] == "metadata_only":
        allowed_missing.add("attachments")
    if (
        not isinstance(missing, list)
        or len(missing) != len(set(missing))
        or any(field not in allowed_missing for field in missing)
    ):
        raise ValueError("Invalid unavailable-field record")
    if "withheld_fields" in row and (
        not isinstance(row["withheld_fields"], list)
        or any(not isinstance(v, str) or len(v) > 256 for v in row["withheld_fields"])
    ):
        raise ValueError("Invalid withheld-field record")
    if len(json_bytes(row)) > MAX_RECORD_BYTES:
        raise ValueError("Oversized record")


def _project(row: dict[str, Any], family: str, scope: dict[str, Any]) -> dict[str, Any]:
    result = {k: row[k] for k in _FIELDS[family] if k in row}
    if family == "assignments":
        result.pop("rubric_settings", None)
        if isinstance(row.get("rubric"), list):
            result["rubric"] = [
                {
                    k: criterion[k]
                    for k in ("id", "points", "criterion_use_range")
                    if k in criterion
                }
                | {
                    "ratings": [
                        {k: rating[k] for k in ("id", "points") if k in rating}
                        for rating in criterion.get("ratings", [])
                    ]
                }
                for criterion in row["rubric"]
                if isinstance(criterion, dict)
            ]
    if family == "submissions" and isinstance(row.get("rubric_assessment"), dict):
        result["rubric_assessment"] = {
            key: {k: value[k] for k in ("points", "rating_id") if k in value}
            for key, value in row["rubric_assessment"].items()
            if isinstance(value, dict)
        }
        if scope["include_comments"]:
            for key, value in row["rubric_assessment"].items():
                if isinstance(value, dict) and "comments" in value:
                    result["rubric_assessment"][key]["comments"] = value["comments"]
    if family == "outcomes" and isinstance(result.get("links"), dict):
        result["links"] = {
            k: numeric_id(v)
            for k, v in result["links"].items()
            if k in {"user", "learning_outcome", "alignment"} and v is not None
        }
    if scope["include_bodies"]:
        for key in {
            "course": ("syllabus_body",),
            "assignments": ("name", "description"),
            "submissions": ("body",),
        }.get(family, ()):
            if key in row:
                result[key] = row[key]
    if scope["include_comments"] and family == "submissions":
        result["submission_comments"] = [
            {
                k: comment[k]
                for k in ("id", "author_id", "comment", "created_at", "edited_at")
                if k in comment
            }
            for comment in row.get("submission_comments") or []
            if isinstance(comment, dict)
        ]
    if (
        scope["include_identities"]
        and family == "enrollments"
        and isinstance(row.get("user"), dict)
    ):
        result["identity"] = {
            k: row["user"][k]
            for k in ("id", "name", "email", "login_id")
            if k in row["user"]
        }
    if (
        family == "submissions"
        and scope["attachments"] == "metadata_only"
        and row.get("attachments") is not None
    ):
        if not isinstance(row["attachments"], list) or any(
            not isinstance(item, dict) for item in row["attachments"]
        ):
            raise ValueError("Invalid attachment metadata")
        result["attachments"] = [
            {
                k: attachment[k]
                for k in ("id", "size", "content-type")
                if k in attachment and attachment[k] is not None
            }
            for attachment in row["attachments"]
        ]
    token = get_config().canvas_api_token
    withheld: list[str] = []

    def clean(value: Any, path: str) -> Any:
        if isinstance(value, str):
            urls = re.findall(r'https?://[^\s<>"\']+', html.unescape(value))
            sensitive = any(
                any(
                    k.lower()
                    in {
                        "token",
                        "access_token",
                        "signature",
                        "api_key",
                        "x-amz-signature",
                        "x-amz-credential",
                        "sig",
                        "x-goog-signature",
                        "x-goog-credential",
                        "googleaccessid",
                        "awsaccesskeyid",
                        "x-amz-security-token",
                        "x-goog-security-token",
                    }
                    for k in parse_qs(urlsplit(url).query)
                )
                for url in urls
            )
            if sensitive or (token and token in value):
                withheld.append(path)
                return None
        if isinstance(value, dict):
            filtered = {}
            for k, v in value.items():
                if token and token in k:
                    withheld.append(path)
                    continue
                filtered[k] = clean(v, f"{path}.{k}")
            return filtered
        if isinstance(value, list):
            return [clean(v, f"{path}.{i}") for i, v in enumerate(value)]
        return value

    cleaned = clean(result, "record")
    if not isinstance(cleaned, dict):
        raise ValueError("Invalid sanitized record")
    result = cleaned
    if withheld:
        result["withheld_fields"] = withheld
    result["unavailable_fields"] = [k for k in _MISSING.get(family, ()) if k not in row]
    if (
        family == "submissions"
        and scope["attachments"] == "metadata_only"
        and row.get("attachments") is None
    ):
        result["unavailable_fields"].append("attachments")
    json_bytes(result)
    _validate_record(result, family, scope)
    return result


def _request(
    manifest: dict[str, Any], unit: dict[str, Any]
) -> tuple[str, dict[str, Any]]:
    family = unit["family"]
    base = canvas_path("courses", manifest["course_id"])
    params: dict[str, Any] = {"per_page": 100, "page": unit["page"]}
    if family == "course":
        return base, {"include[]": ["syllabus_body"]} if manifest["scope"][
            "include_bodies"
        ] else {}
    if family in {"submissions", "peer_reviews"}:
        path = base + canvas_path(
            "assignments", numeric_id(unit["assignment_id"]), family
        )
        if family == "submissions":
            params["include[]"] = ["rubric_assessment"] + (
                ["submission_comments"] if manifest["scope"]["include_comments"] else []
            )
        return path, params
    if family == "enrollments":
        params.update(
            {
                "type[]": "StudentEnrollment",
                "state[]": manifest["scope"]["enrollment_states"],
            }
        )
    return base + "/" + ("outcome_results" if family == "outcomes" else family), params


def _dependent_units(manifest: dict[str, Any], store: ArtifactStore) -> None:
    if manifest["units"].get("assignments", {}).get("state") != "complete":
        return
    selected = set(manifest["scope"]["assignment_ids"])
    found = set(_assignment_ids(manifest, store))
    if len(found) > 1000:
        manifest["units"]["assignments"].update(state="failed", error="scope_limit")
        return
    if selected and selected - found:
        manifest["units"]["assignments"].update(
            state="unavailable", error="selected_assignments_unavailable"
        )
        return
    for family in {"submissions", "peer_reviews"} & set(manifest["scope"]["families"]):
        for assignment in _assignment_ids(manifest, store):
            manifest["units"].setdefault(
                f"{family}-{assignment}",
                {
                    "family": family,
                    "assignment_id": assignment,
                    "state": "pending",
                    "page": "1",
                    "files": [],
                    "count": 0,
                    "missing_fields": {},
                    "seen_pages": [],
                },
            )


async def collect_pages(
    store: ArtifactStore, manifest: dict[str, Any], page_budget: int
) -> dict[str, Any]:
    attempted: set[str] = set()
    for _ in range(page_budget):
        _dependent_units(manifest, store)
        keys = [
            k
            for k, u in manifest["units"].items()
            if u["state"] != "complete"
            and k not in attempted
            and u.get("error")
            not in {"cursor_expired", "scope_limit", "selected_assignments_unavailable"}
        ]
        if not keys:
            break
        key = keys[0]
        unit = manifest["units"][key]
        path, params = _request(manifest, unit)
        pagination: dict[str, str | None] = {}
        result = await make_canvas_request(
            "get",
            path,
            params=params,
            skip_anonymization=True,
            _pagination=pagination,
            _max_response_bytes=MAX_FILE_BYTES,
        )
        if isinstance(result, dict) and "error" in result:
            status = getattr(result, "status_code", None)
            unit.update(
                state="unavailable" if status in {401, 403, 404} else "failed",
                error="cursor_expired"
                if status in {400, 404, 410} and unit["page"] != "1"
                else "read_unavailable",
            )
            attempted.add(key)
            manifest["state"] = "partial"
            checkpoint(store, manifest)
            if status in {401, 403}:
                break
            continue
        family = unit["family"]
        rows = (
            [result]
            if family == "course"
            else result.get("outcome_results")
            if family == "outcomes" and isinstance(result, dict)
            else result
        )
        try:
            if (
                not isinstance(rows, list)
                or len(rows) > 1000
                or any(not isinstance(r, dict) for r in rows)
            ):
                raise ValueError("Malformed read response")
            existing = {
                record_key(family, r): hashlib.sha256(json_bytes(r)).hexdigest()
                for filename in unit["files"]
                for r in verified_records(store, filename, manifest["files"][filename])
            }
            saved = []
            for row in rows:
                record = _project(row, family, manifest["scope"])
                identity = record_key(family, record)
                if (
                    "course_id" in record
                    and numeric_id(record["course_id"]) != manifest["course_id"]
                ):
                    raise ValueError("Cross-course response")
                if family == "course" and identity != manifest["course_id"]:
                    raise ValueError("Wrong course response")
                if (
                    family == "submissions"
                    and numeric_id(record["assignment_id"]) != unit["assignment_id"]
                ):
                    raise ValueError("Wrong assignment response")
                if (
                    family == "assignments"
                    and manifest["scope"]["assignment_ids"]
                    and identity not in manifest["scope"]["assignment_ids"]
                ):
                    continue
                digest = hashlib.sha256(json_bytes(record)).hexdigest()
                if identity in existing:
                    if (
                        existing[identity] != digest
                        and key not in manifest["consistency_issues"]
                    ):
                        manifest["consistency_issues"].append(key)
                    continue
                existing[identity] = digest
                saved.append(record)
                if len(existing) > 100000:
                    raise ValueError("Record count limit exceeded")
            encoded = [json_bytes(r) + b"\n" for r in saved]
            if any(len(line) > MAX_RECORD_BYTES for line in encoded):
                raise ValueError("Record byte limit exceeded")
            if sum(len(line) for line in encoded) > MAX_FILE_BYTES:
                raise ValueError("Page byte limit exceeded")
            data = b"".join(encoded)
            if (
                sum(v["bytes"] for v in manifest["files"].values()) + len(data)
                > MAX_ARCHIVE_BYTES
            ):
                raise ValueError("Archive byte limit exceeded")
            next_page = None
            if pagination.get("next"):
                target = urlsplit(str(pagination["next"]))
                expected = urlsplit(get_config().canvas_api_url.rstrip("/") + path)
                if (
                    (target.scheme, target.netloc, target.path)
                    != (expected.scheme, expected.netloc, expected.path)
                    or target.fragment
                    or target.username
                ):
                    raise ValueError("Unsafe pagination")
                query = parse_qs(target.query)
                values = query.get("page", [])
                if len(values) != 1 or not re.fullmatch(
                    r"[A-Za-z0-9_.:=-]{1,256}", values[0]
                ):
                    raise ValueError("Unsupported pagination cursor")
                next_page = values[0]
                if next_page in unit["seen_pages"] or next_page == unit["page"]:
                    raise ValueError("Pagination cycle")
            if data:
                name = f"records-{uuid.uuid4().hex}.jsonl"
                manifest["files"][name] = store.write(name, data)
                unit["files"].append(name)
                unit["count"] += len(saved)
                for record in saved:
                    for field in record["unavailable_fields"]:
                        unit["missing_fields"][field] = (
                            unit["missing_fields"].get(field, 0) + 1
                        )
            unit["seen_pages"].append(unit["page"])
            unit.pop("error", None)
            unit["state"] = "collecting" if next_page else "complete"
            if next_page:
                unit["page"] = next_page
            _dependent_units(manifest, store)
        except (ValueError, KeyError, TypeError, OverflowError):
            unit.update(state="failed", error="invalid_or_oversized_response")
            attempted.add(key)
        states = {u["state"] for u in manifest["units"].values()}
        manifest["state"] = (
            "complete"
            if states == {"complete"} and not manifest["consistency_issues"]
            else "partial"
            if states & {"failed", "unavailable"} or manifest["consistency_issues"]
            else "interrupted"
        )
        checkpoint(store, manifest)
    return snapshot_summary(manifest)


def snapshot_summary(manifest: dict[str, Any]) -> dict[str, Any]:
    families: dict[str, Any] = {}
    for unit in manifest["units"].values():
        item = families.setdefault(
            unit["family"], {"records": 0, "states": {}, "unavailable_fields": {}}
        )
        item["records"] += unit["count"]
        item["states"][unit["state"]] = item["states"].get(unit["state"], 0) + 1
        for field, count in unit["missing_fields"].items():
            item["unavailable_fields"][field] = (
                item["unavailable_fields"].get(field, 0) + count
            )
    for family in manifest["scope"]["families"]:
        if family not in families:
            dependency_complete = (
                manifest["units"].get("assignments", {}).get("state") == "complete"
            )
            families[family] = {
                "records": 0,
                "states": {"complete" if dependency_complete else "unavailable": 1},
                "unavailable_fields": {},
            }
    return {
        "schema_version": 1,
        "state": manifest["state"],
        "families": families,
        "started_at": manifest["started_at"],
        "observed_until": manifest["observed_until"],
        "consistency": manifest["consistency"],
        "changes_detected": bool(manifest["consistency_issues"]),
        "resume_possible": manifest["generation"] < 9999
        and any(
            u["state"] != "complete"
            and u.get("error")
            not in {"cursor_expired", "scope_limit", "selected_assignments_unavailable"}
            for u in manifest["units"].values()
        ),
        "recapture_required": bool(manifest["consistency_issues"])
        or manifest["generation"] >= 9999
        or any(
            u.get("error")
            in {"cursor_expired", "scope_limit", "selected_assignments_unavailable"}
            for u in manifest["units"].values()
        ),
        "canvas_writes": 0,
    }
