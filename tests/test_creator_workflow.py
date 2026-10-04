import json
from urllib.parse import parse_qs

import httpx
import pytest
from fastmcp import Client, FastMCP

from canvas_mcp.core import cache
from canvas_mcp.core import client as canvas_client
from canvas_mcp.core.tool_policy import apply_tool_policy, resolve_tool_policy
from canvas_mcp.server import register_all_tools


@pytest.mark.asyncio
async def test_creator_builds_draft_assignment_group_and_module_over_mcp(monkeypatch):
    monkeypatch.setenv("CANVAS_API_URL", "https://canvas.invalid/api/v1")
    monkeypatch.setenv("CANVAS_API_TOKEN", "workflow-dummy-token")
    monkeypatch.setenv("CANVAS_ROLE", "creator")
    monkeypatch.setenv("ENABLE_DATA_ANONYMIZATION", "false")
    monkeypatch.setattr(cache, "course_code_to_id_cache", {})
    monkeypatch.setattr(cache, "id_to_course_code_cache", {})
    assignment = {}
    module = {}
    items = []
    writes = []

    def respond(request):
        path = request.url.path.removeprefix("/api/v1")
        method = request.method
        if method != "GET":
            writes.append((method, path))
        if (method, path) == ("GET", "/courses"):
            return httpx.Response(200, json=[{"id": 42, "course_code": "COURSE/42"}])
        if (method, path) == ("POST", "/courses/42/assignment_groups"):
            assert json.loads(request.content) == {"name": "Practice"}
            return httpx.Response(201, json={"id": 12, "name": "Practice"})
        if (method, path) == ("POST", "/courses/42/assignments"):
            body = json.loads(request.content)["assignment"]
            assert body["assignment_group_id"] == "12"
            assert body["published"] is False
            assert body["submission_types"] == ["online_text_entry"]
            assignment.update(id=34, **body)
            return httpx.Response(201, json=assignment)
        if (method, path) == ("PUT", "/courses/42/assignments/34"):
            assert json.loads(request.content) == {"assignment": {"due_at": None}}
            assignment["due_at"] = None
            return httpx.Response(200, json=assignment)
        if (method, path) == ("POST", "/courses/42/modules"):
            assert parse_qs(request.content.decode()) == {
                "module[name]": ["Week 1"], "module[published]": ["false"]
            }
            module.update(id=56, name="Week 1", published=False, position=1)
            return httpx.Response(201, json=module)
        if (method, path) == ("POST", "/courses/42/modules/56/items"):
            assert parse_qs(request.content.decode()) == {
                "module_item[type]": ["Assignment"],
                "module_item[content_id]": ["34"],
            }
            item = {"id": 78, "type": "Assignment", "content_id": 34,
                    "title": assignment["name"], "published": False, "position": 1}
            items.append(item)
            return httpx.Response(201, json=item)
        if (method, path) == ("GET", "/courses/42/modules"):
            assert request.url.params.get("include[]") == "items"
            return httpx.Response(200, json=[{**module, "items": items, "items_count": 1}])
        raise AssertionError(f"Unexpected request: {method} {path}")

    server = FastMCP("creator-workflow")
    register_all_tools(server, role="creator")
    await apply_tool_policy(server, resolve_tool_policy(None, "stdio"))
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        monkeypatch.setattr(canvas_client, "_get_http_client", lambda: http)
        async with Client(server) as mcp:
            names = {tool.name for tool in await mcp.list_tools()}
            assert {"create_assignment_group", "create_module", "get_course_structure"} <= names
            assert not {"list_submissions", "list_users", "execute_typescript", "send_conversation"} & names
            calls = [
                ("create_assignment_group", {"name": "Practice"}),
                ("create_assignment", {
                    "name": "Exercise", "assignment_group_id": 12,
                    "submission_types": "online_text_entry",
                    "due_at": "2026-10-10T12:00:00Z",
                }),
                ("create_module", {"name": "Week 1", "published": False}),
                ("add_module_item", {"module_id": 56, "item_type": "Assignment", "content_id": 34}),
                ("update_assignment", {"assignment_id": 34, "clear_due_at": True}),
            ]
            for name, arguments in calls:
                result = await mcp.call_tool(name, {"course_identifier": "COURSE/42", **arguments})
                assert not result.is_error
            result = await mcp.call_tool("get_course_structure", {"course_identifier": 42})
            structure = json.loads(result.content[0].text)
    assert assignment["due_at"] is None
    assert assignment["assignment_group_id"] == "12"
    assert assignment["published"] is False
    assert structure["summary"]["total_modules"] == 1
    assert structure["summary"]["unpublished_modules"] == 1
    assert structure["summary"]["total_items"] == 1
    assert "UNTRUSTED CANVAS CONTENT" in structure["modules"][0]["name"]
    assert structure["modules"][0]["items"][0]["content_id"] == 34
    assert len(writes) == 5
