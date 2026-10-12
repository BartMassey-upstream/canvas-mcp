"""Per-request credential context for HTTP transport.

When the server runs in HTTP mode, each request carries its own Canvas API
token via the X-Canvas-Token header. The Canvas API URL is pinned by server
configuration (CANVAS_API_URL), never supplied by the client. This module
uses Python's contextvars to thread the per-request token through the async
call stack without modifying any tool signatures.

In stdio mode, the ContextVar remains unset (None), and the client falls back
to the global local configuration (``~/.canvas-mcp`` or the environment). To
keep that fallback from leaking the server's own token in HTTP mode, an
additional ``_http_request_active`` marker distinguishes "HTTP request with no
token" (must fail closed) from "stdio mode" (local fallback is intended).
"""

from contextvars import ContextVar
from dataclasses import dataclass, field


@dataclass(frozen=True)
class RequestCredentials:
    """Canvas API credentials for a single HTTP request."""

    api_token: str = field(repr=False)
    api_url: str


@dataclass
class CourseMetadata:
    """Course aliases and display labels belonging to one HTTP request.

    An alias mapped to None is ambiguous and must never select a target.
    Credential replacement discards this state, including inherited labels.
    """

    aliases: dict[str, str | None] = field(default_factory=dict)
    labels: dict[str, str] = field(default_factory=dict)
    loaded: bool = False


_request_course_metadata: ContextVar[CourseMetadata | None] = ContextVar(
    "request_course_metadata", default=None
)


def get_request_course_metadata() -> CourseMetadata:
    """Return lazily initialized metadata for the current HTTP context."""
    metadata = _request_course_metadata.get()
    if metadata is None:
        metadata = CourseMetadata()
        _request_course_metadata.set(metadata)
    return metadata


_request_credentials: ContextVar[RequestCredentials | None] = ContextVar(
    "request_credentials", default=None
)

_http_request_active: ContextVar[bool] = ContextVar(
    "http_request_active", default=False
)


def get_request_credentials() -> RequestCredentials | None:
    """Get the current request's Canvas credentials, or None for stdio mode."""
    return _request_credentials.get()


def set_request_credentials(creds: RequestCredentials) -> None:
    """Set Canvas credentials for the current async context."""
    _request_credentials.set(creds)
    _request_course_metadata.set(None)


def clear_request_credentials() -> None:
    """Clear credentials after request completes."""
    _request_credentials.set(None)
    _request_course_metadata.set(None)


def is_http_request_active() -> bool:
    """Return True while handling an HTTP request.

    Used to fail closed: in HTTP mode a missing per-request token must never
    fall back to the server's own credentials.
    """
    return _http_request_active.get()


def set_http_request_active(active: bool = True) -> None:
    """Mark whether the current async context is handling an HTTP request."""
    _http_request_active.set(active)
    _request_course_metadata.set(None)


def clear_http_request_context() -> None:
    """Clear all per-request HTTP context after the request completes."""
    _request_credentials.set(None)
    _http_request_active.set(False)
    _request_course_metadata.set(None)
