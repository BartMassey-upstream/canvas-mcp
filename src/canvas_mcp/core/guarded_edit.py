"""Preflight and exact read-back verification for existing-content edits.

The optional guards detect stale caller snapshots; every edit still fetches its
identity and verifies the full written body and all requested fields. These
tools do not use an atomic conditional PUT: another editor can change content
after preflight and before the PUT. These checks do not lock that interval.
Provenance fencing belongs at the output boundary, never in bodies sent to Canvas.
"""

import datetime
import hashlib
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from .untrusted_content import (
    FENCE_LEAK_ERROR,
    contains_fence_markers,
    fence_untrusted_inline,
    format_canvas_error,
)
from .write_confirmation import unconfirmed_write_warning
from .write_outcome import NO_WRITE_STATUSES, RequestFailure, WriteOutcome

NOTHING_WRITTEN = "Nothing was written."
_SHA256_HEX = re.compile(r"[0-9a-fA-F]{64}")
_MISSING = object()


@dataclass(frozen=True)
class BodyGuard:
    """Optional preconditions and a single exact fragment substitution."""

    expect_updated_at: str | None = None
    find: str | None = None
    replace: str | None = None
    require: list[str] | None = None
    expect_body_sha256: str | None = None

    @property
    def active(self) -> bool:
        """Whether any guard argument was supplied."""
        return any(
            value is not None
            for value in (
                self.expect_updated_at,
                self.expect_body_sha256,
                self.find,
                self.replace,
                self.require,
            )
        )

    @property
    def fragment(self) -> bool:
        """Whether either fragment argument was supplied."""
        return self.find is not None or self.replace is not None


def body_sha256(body: str | None) -> str:
    """Hash exact raw UTF-8 body bytes; an explicit Canvas null means empty."""
    if body is not None and not isinstance(body, str):
        raise TypeError("Canvas body must be a string or explicit null")
    return hashlib.sha256(
        (body if body is not None else "").encode("utf-8")
    ).hexdigest()


def parse_timestamp(value: str | None) -> datetime.datetime | None:
    """Parse an ISO instant in UTC, retaining fractional-second precision."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=datetime.UTC)
        return parsed.astimezone(datetime.UTC)
    except (ValueError, OverflowError):
        return None


def validate_guard(guard: BodyGuard, body_param: str, body: object) -> str | None:
    """Validate caller input before any request, without echoing input text."""
    if body is not None and not isinstance(body, str):
        return "❌ The full body must be a string. " + NOTHING_WRITTEN
    if body is not None and contains_fence_markers(body):
        return FENCE_LEAK_ERROR
    if guard.fragment:
        if not isinstance(guard.find, str) or not isinstance(guard.replace, str):
            return (
                "❌ find and replace must be supplied together as strings. "
                + NOTHING_WRITTEN
            )
        if not guard.find.strip():
            return "❌ find must contain non-whitespace text. " + NOTHING_WRITTEN
        if body is not None:
            return (
                f"❌ Supply either {body_param} (the full body) or find/replace "
                f"(one fragment), not both. {NOTHING_WRITTEN}"
            )
        if contains_fence_markers(guard.find) or contains_fence_markers(guard.replace):
            return FENCE_LEAK_ERROR
    if guard.require is not None:
        if not isinstance(guard.require, list) or any(
            not isinstance(item, str) or not item for item in guard.require
        ):
            return "❌ require must be a list of non-empty strings. " + NOTHING_WRITTEN
        if any(contains_fence_markers(item) for item in guard.require):
            return FENCE_LEAK_ERROR
    if (
        guard.expect_updated_at is not None
        and parse_timestamp(guard.expect_updated_at) is None
    ):
        return "❌ expect_updated_at must be an ISO 8601 timestamp. " + NOTHING_WRITTEN
    if guard.expect_body_sha256 is not None and (
        not isinstance(guard.expect_body_sha256, str)
        or not _SHA256_HEX.fullmatch(guard.expect_body_sha256.strip())
    ):
        return (
            "❌ expect_body_sha256 must be a 64-character hex SHA-256. "
            + NOTHING_WRITTEN
        )
    return None


def check_updated_at(current: dict[str, Any], expected: str, what: str) -> str | None:
    """Refuse absent, malformed, or stale updated_at metadata."""
    actual = parse_timestamp(current.get("updated_at"))
    if actual is None:
        return "❌ Cannot check changes: no usable updated_at. " + NOTHING_WRITTEN
    if actual != parse_timestamp(expected):
        return (
            "❌ The target changed since it was read. Read it again before retrying. "
            + NOTHING_WRITTEN
        )
    return None


def check_body_hash(current_body: str | None, expected: str, what: str) -> str | None:
    """Compare a caller's exact body hash with the fresh body."""
    if body_sha256(current_body) != expected.strip().lower():
        return (
            "❌ The body changed since it was read. Read it again before retrying. "
            + NOTHING_WRITTEN
        )
    return None


def check_require(body: str, require: list[str] | None, what: str) -> str | None:
    """Refuse missing requirements without echoing author-controlled text."""
    if any(item not in body for item in (require or [])):
        return "❌ The current body is missing required text. " + NOTHING_WRITTEN
    return None


def apply_find_replace(
    body: str, find: str, replace: str, what: str
) -> tuple[str | None, str | None]:
    """Replace exactly one literal occurrence, including HTML and whitespace."""
    count = body.count(find)
    if count != 1:
        return (
            None,
            f"❌ find must match exactly once; matched {count} times. {NOTHING_WRITTEN}",
        )
    return body.replace(find, replace, 1), None


def _body(current: dict[str, Any], field: str) -> tuple[str | None, bool]:
    """Distinguish an absent/malformed body from an explicitly empty body."""
    value = current.get(field, _MISSING)
    if value is None:
        return "", True
    return (value, True) if isinstance(value, str) else (None, False)


def prepare_body(
    guard: BodyGuard,
    current: dict[str, Any],
    body_field: str,
    full_body: str | None,
    what: str,
) -> tuple[str | None, str | None]:
    """Check a fresh snapshot and return the exact complete body to send."""
    error = validate_guard(guard, body_field, full_body)
    if error:
        return None, error
    if guard.expect_updated_at is not None:
        error = check_updated_at(current, guard.expect_updated_at, what)
        if error:
            return None, error
    current_body, usable = _body(current, body_field)
    needed = (
        full_body is not None
        or guard.fragment
        or guard.expect_body_sha256 is not None
        or guard.require is not None
    )
    if needed and not usable:
        return (
            None,
            "❌ Canvas returned a missing or malformed body. " + NOTHING_WRITTEN,
        )
    if guard.expect_body_sha256 is not None:
        error = check_body_hash(current_body, guard.expect_body_sha256, what)
        if error:
            return None, error
    if guard.require is not None:
        assert current_body is not None
        error = check_require(current_body, guard.require, what)
        if error:
            return None, error
    if guard.fragment:
        assert (
            current_body is not None
            and guard.find is not None
            and guard.replace is not None
        )
        return apply_find_replace(current_body, guard.find, guard.replace, what)
    return full_body, None


BODY_MISMATCH = "the stored body differs from the exact full body sent; Canvas dropped or rewrote content"


def body_readback_failure(expected_body: str, stored_body: object) -> str | None:
    """Prove exact whole-body equality, preserving attributes and whitespace."""
    if stored_body is not None and not isinstance(stored_body, str):
        return "the read-back body is malformed"
    return (
        None
        if expected_body == (stored_body if stored_body is not None else "")
        else BODY_MISMATCH
    )


def _numeric_id(value: Any) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        if value <= 0:
            return None
        try:
            return str(value)
        except ValueError:
            return None
    if isinstance(value, str) and re.fullmatch(r"[0-9]+", value):
        return value.lstrip("0") or None
    return None


def _same_value(field: str, sent: Any, got: Any) -> bool:
    """Compare requested values with date, ID, set-list and nested semantics."""
    if got is _MISSING:
        return False
    if field.endswith("_at"):
        if sent is None or sent == "":
            return got is None or got == ""
        instant = parse_timestamp(sent)
        return instant is not None and instant == parse_timestamp(got)
    if isinstance(sent, bool) or isinstance(got, bool):
        return type(sent) is bool and type(got) is bool and sent == got
    if isinstance(sent, dict):
        return isinstance(got, dict) and all(
            _same_value(key, value, got.get(key, _MISSING))
            for key, value in sent.items()
        )
    if isinstance(sent, list):
        if not isinstance(got, list) or len(sent) != len(got):
            return False
        remaining = list(got)
        for item in sent:
            match = next(
                (
                    i
                    for i, other in enumerate(remaining)
                    if _same_value(field.removesuffix("s"), item, other)
                ),
                None,
            )
            if match is None:
                return False
            remaining.pop(match)
        return True
    if sent is None or got is None:
        return sent is got
    if isinstance(sent, (int, float)) or field == "id" or field.endswith("_id"):
        try:
            return Decimal(str(sent)) == Decimal(str(got))
        except InvalidOperation:
            return False
    return type(sent) is type(got) and sent == got


def _path_value(obj: dict[str, Any], path: str) -> Any:
    value: Any = obj
    for component in path.split("."):
        if not isinstance(value, dict) or component not in value:
            return _MISSING
        value = value[component]
    return value


def _identity_failure(obj: dict[str, Any], identity: dict[str, Any]) -> bool:
    return any(
        _numeric_id(expected) is None
        or _numeric_id(obj.get(field)) != _numeric_id(expected)
        for field, expected in identity.items()
    )


def readback_failure(
    *,
    guard: BodyGuard,
    before: dict[str, Any],
    after: Any,
    body_field: str,
    written_body: str | None,
    requested: dict[str, Any],
    has_updated_at: bool,
    identity: dict[str, Any] | None = None,
    identity_field: str = "id",
) -> str | None:
    """Prove identity, the full written body and every requested field.

    updated_at need not advance: a same-second or no-op write is valid when
    the actual requested state reads back exactly. Guards were checked before
    dispatch and cannot establish atomicity against a concurrent editor.
    """
    if not isinstance(after, dict) or "error" in after:
        return "the read-back after the write failed"
    expected_identity = (
        identity
        if identity is not None
        else {identity_field: before.get(identity_field)}
    )
    if (
        not expected_identity
        or _identity_failure(before, expected_identity)
        or _identity_failure(after, expected_identity)
    ):
        return "the read-back target identity could not be verified"
    expected_body = written_body
    if expected_body is None:
        # A metadata edit must not silently discard an available body. Some
        # metadata endpoints omit bodies; absence there is not guessed empty.
        before_body, before_usable = _body(before, body_field)
        if before_usable:
            expected_body = before_body
    if expected_body is not None:
        stored, usable = _body(after, body_field)
        if not usable:
            return "the read-back has a missing or malformed body"
        reason = body_readback_failure(expected_body, stored)
        if reason:
            return reason
    for field, sent in requested.items():
        if field == body_field and written_body is not None:
            continue
        if not _same_value(field.rsplit(".", 1)[-1], sent, _path_value(after, field)):
            return "one or more requested fields did not read back with the value sent"
    return None


def _failed(response: Any) -> bool:
    return not isinstance(response, dict) or "error" in response


def _diagnostic(response: Any) -> str:
    if isinstance(response, BaseException):
        # Exception messages can include signed URLs or request credentials.
        return type(response).__name__
    if isinstance(response, RequestFailure) and response.status_code is not None:
        return f"HTTP error: {response.status_code}"
    detail = response.get("error") if isinstance(response, dict) else response
    return format_canvas_error(detail)


def _safe_facts(facts: dict[str, Any]) -> dict[str, str]:
    """Bound and fence even caller-supplied labels and metadata at output."""
    return {
        fence_untrusted_inline(
            str(label)[:100], "edit fact label"
        ): fence_untrusted_inline(str(value)[:500], "edit fact")
        for label, value in facts.items()
        if value is not None
    }


def _warning(what: str, reason: str, facts: dict[str, Any]) -> str:
    return unconfirmed_write_warning(
        "the existing-content update",
        {"Reason": reason, **_safe_facts(facts)},
        "The write outcome is unconfirmed. Do not retry automatically. Inspect the target in Canvas before retrying.",
    )


async def run_guarded_write(
    guard: BodyGuard,
    *,
    what: str,
    body_field: str,
    full_body: str | None,
    fetch: Callable[[], Awaitable[Any]],
    write: Callable[[str | None], Awaitable[Any]],
    refetch: Callable[[Any], Awaitable[Any]],
    requested: dict[str, Any],
    facts: dict[str, Any],
    has_updated_at: bool = True,
    identity: dict[str, Any] | None = None,
    identity_field: str = "id",
) -> str:
    """Preflight, issue one PUT, then verify; also used with inactive guards.

    ``refetch`` receives the write response to support renamed page slugs.
    Fetch/write exceptions and unknown failures are never automatically retried.
    Only transport evidence can establish that a dispatched write did not occur.
    """
    error = validate_guard(guard, body_field, full_body)
    if error:
        return error
    if full_body is None and not guard.fragment and not requested:
        return "❌ No fields were supplied to update. " + NOTHING_WRITTEN
    if guard.expect_updated_at is not None and not has_updated_at:
        return (
            "❌ This target has no updated_at; use expect_body_sha256. "
            + NOTHING_WRITTEN
        )
    if identity is not None and (
        not identity or any(_numeric_id(value) is None for value in identity.values())
    ):
        return "❌ The expected target identity is invalid. " + NOTHING_WRITTEN
    try:
        current = await fetch()
    except Exception as exc:
        return f"❌ Could not fetch the target before editing. {NOTHING_WRITTEN}\n{_diagnostic(exc)}"
    if _failed(current):
        return f"❌ Could not fetch the target before editing. {NOTHING_WRITTEN}\n{_diagnostic(current)}"
    expected_identity = (
        identity
        if identity is not None
        else {identity_field: current.get(identity_field)}
    )
    if _identity_failure(current, expected_identity):
        return (
            "❌ The target identity could not be verified before editing. "
            + NOTHING_WRITTEN
        )
    body, error = prepare_body(guard, current, body_field, full_body, what)
    if error:
        return error
    try:
        response = await write(body)
    except Exception as exc:
        return _warning(
            what, "the write raised an exception: " + _diagnostic(exc), facts
        )
    if _failed(response):
        if isinstance(response, RequestFailure) and (
            response.outcome == WriteOutcome.NOT_DISPATCHED
            or (
                response.outcome == WriteOutcome.REJECTED
                and response.status_code in NO_WRITE_STATUSES
            )
        ):
            return f"❌ Canvas rejected the update. {NOTHING_WRITTEN}\n{_diagnostic(response)}"
        return _warning(
            what,
            "the write returned an uncertain failure: " + _diagnostic(response),
            facts,
        )
    try:
        after = await refetch(response)
    except Exception as exc:
        return _warning(what, "the read-back failed: " + _diagnostic(exc), facts)
    reason = readback_failure(
        guard=guard,
        before=current,
        after=after,
        body_field=body_field,
        written_body=body,
        requested=requested,
        has_updated_at=has_updated_at,
        identity=expected_identity,
        identity_field=identity_field,
    )
    if reason:
        if _failed(after):
            reason += ": " + _diagnostic(after)
        return _warning(what, reason, facts)
    lines = [
        "✅ Updated the existing content; confirmed by reading the target back from Canvas."
    ]
    lines += [f"  {label}: {value}" for label, value in _safe_facts(facts).items()]
    stored_body, usable_body = _body(after, body_field)
    if usable_body:
        lines.append(f"  Body SHA-256: {body_sha256(stored_body)}")
    if has_updated_at and parse_timestamp(after.get("updated_at")) is not None:
        lines.append(f"  Updated (raw): {after['updated_at']}")
    return "\n".join(lines)
