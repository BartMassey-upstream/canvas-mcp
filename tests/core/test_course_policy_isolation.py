"""Course policy reads must belong to the current HTTP caller."""

import time
from unittest.mock import AsyncMock, patch

import pytest

from canvas_mcp.core import course_policy as policy
from canvas_mcp.core.credentials import (
    clear_http_request_context,
    set_http_request_active,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("body, expected", [(None, False), ("agent_writes: deny", False),
                                           ("agent_writes: allow", True)])
async def test_http_policy_ignores_and_preserves_stdio_cache(body, expected):
    granted = policy.CoursePolicy(True, None, "private instructor note", "course_artifact")
    original = (time.monotonic() + 60, granted)
    policy._policy_cache["123"] = original
    set_http_request_active()
    try:
        status = "error" if body is None else "ok"
        with patch.object(policy, "_read_policy_text", new=AsyncMock(return_value=(body, status))) as read:
            result = await policy.get_course_policy(123)
        read.assert_awaited_once_with("123")
        assert result.allow_writes is expected
        assert result.note != "private instructor note"
        assert policy._policy_cache["123"] == original
    finally:
        clear_http_request_context()
        policy.reset_policy_cache()


@pytest.mark.asyncio
async def test_http_policy_does_not_seed_later_stdio_verdict():
    policy.reset_policy_cache()
    set_http_request_active()
    try:
        with patch.object(policy, "_read_policy_text", new=AsyncMock(return_value=("agent_writes: allow", "ok"))):
            assert (await policy.get_course_policy(123)).allow_writes
        assert policy._policy_cache == {}
    finally:
        clear_http_request_context()
        policy.reset_policy_cache()
