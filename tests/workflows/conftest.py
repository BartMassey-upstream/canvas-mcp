"""Shared workflow fixtures, with no real credentials or network fallback."""

from contextlib import asynccontextmanager

import httpx
import pytest
from fastmcp import Client, FastMCP
from synthetic_canvas import SyntheticCanvas

from canvas_mcp.core import cache
from canvas_mcp.core import client as canvas_client
from canvas_mcp.core.config import reset_config
from canvas_mcp.core.tool_policy import apply_tool_policy, resolve_tool_policy
from canvas_mcp.server import register_all_tools


@pytest.fixture
def workflow_session(monkeypatch):
    @asynccontextmanager
    async def session(actor="instructor", profile="educator", allowed_write_tools=None):
        monkeypatch.setenv("CANVAS_API_URL", "https://canvas.invalid/api/v1")
        monkeypatch.setenv("CANVAS_API_TOKEN", "stage3-dummy-token")
        monkeypatch.setenv("CANVAS_ROLE", profile)
        monkeypatch.setenv("ENABLE_DATA_ANONYMIZATION", "false")
        monkeypatch.setenv("STUDENT_WRITE_TOOLS", "")
        monkeypatch.setenv("EXECUTE_TYPESCRIPT_ENABLED", "false")
        monkeypatch.setattr(cache, "course_code_to_id_cache", {})
        monkeypatch.setattr(cache, "id_to_course_code_cache", {})
        reset_config()
        canvas = SyntheticCanvas(actor)
        server = FastMCP("synthetic-workflow")
        register_all_tools(server, role=profile)
        await apply_tool_policy(
            server, resolve_tool_policy(allowed_write_tools, "stdio")
        )
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(canvas.respond)
        ) as http:
            monkeypatch.setattr(canvas_client, "_get_http_client", lambda: http)
            async with Client(server) as mcp:
                yield mcp, canvas
        assert not canvas.writes

    return session
