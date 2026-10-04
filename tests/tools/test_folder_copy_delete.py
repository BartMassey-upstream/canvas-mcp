import re
from unittest.mock import AsyncMock, patch

import pytest
from fastmcp import FastMCP

from canvas_mcp.tools.files import register_educator_file_tools


def folder(identity, parent=1, **extra):
    return {"id": identity, "parent_folder_id": parent, "name": f"Folder {identity}",
            "context_type": "Course", "context_id": 42, "for_submissions": False, **extra}


async def tool(name):
    server = FastMCP("folder-copy-delete")
    register_educator_file_tools(server)
    return next(item.fn for item in await server.list_tools() if item.name == name)


@pytest.fixture
def api():
    with patch("canvas_mcp.tools.files.get_course_id", new=AsyncMock(return_value="42")), patch(
        "canvas_mcp.tools.files.make_canvas_request", new_callable=AsyncMock
    ) as request, patch("canvas_mcp.tools.files.fetch_all_paginated_results", new=AsyncMock(return_value=[])) as fetch:
        yield request, fetch


@pytest.mark.asyncio
async def test_empty_folder_delete_preview_confirm_and_one_use(api):
    request, _ = api
    request.side_effect = lambda method, *args, **kwargs: folder(2) if method == "get" else {}
    delete = await tool("delete_course_folder")
    preview = await delete("42", "2")
    token = re.search(r"Confirmation token: ([\w.-]+)", preview)
    if token is None:
        token = re.search(r'"confirmation_token":\s*"([\w.-]+)"', preview)
    assert token is not None, preview
    assert all(call.args[0] == "get" for call in request.await_args_list)
    confirmed = await delete("42", "2", confirmation_token=token[1])
    assert "folder deleted" in confirmed
    writes = [call for call in request.await_args_list if call.args[0] == "delete"]
    assert len(writes) == 1
    assert writes[0].kwargs["params"] == {"force": "false"}
    await delete("42", "2", confirmation_token=token[1])
    assert len([call for call in request.await_args_list if call.args[0] == "delete"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("source,contents", [
    (folder(1, parent=None), []),
    (folder(2, for_submissions=True), []),
    (folder(2, context_id=99), []),
    (folder(2), [{"id": 7}]),
    (folder(2), {"error": "forbidden"}),
])
async def test_deletion_refuses_root_content_and_unverified_folders(api, source, contents):
    request, fetch = api
    request.return_value = source
    fetch.return_value = contents
    result = await (await tool("delete_course_folder"))("42", source["id"])
    assert result.startswith("Error")
    assert all(call.args[0] == "get" for call in request.await_args_list)


@pytest.mark.asyncio
async def test_empty_folder_change_invalidates_preview(api):
    request, _ = api
    request.return_value = folder(2)
    delete = await tool("delete_course_folder")
    preview = await delete("42", 2)
    token = re.search(r"Confirmation token: ([\w.-]+)", preview)
    if token is None:
        token = re.search(r'"confirmation_token":\s*"([\w.-]+)"', preview)
    assert token is not None, preview
    request.return_value = folder(2, name="changed")
    result = await delete("42", 2, confirmation_token=token[1])
    assert "not deleted" in result or "Nothing" in result
    assert all(call.args[0] == "get" for call in request.await_args_list)


@pytest.mark.asyncio
async def test_copy_folder_validates_tree_and_target(api):
    request, fetch = api
    request.side_effect = [folder(2), folder(3), folder(4, parent=3)]
    fetch.return_value = [folder(2), folder(3), folder(5, parent=2)]
    result = await (await tool("copy_course_folder"))("42", 2, 3)
    assert "folder copied" in result
    assert "UNTRUSTED CANVAS CONTENT" in result
    assert request.await_args.args == ("post", "/folders/3/copy_folder")
    assert request.await_args.kwargs["data"] == {"source_folder_id": 2}


@pytest.mark.asyncio
@pytest.mark.parametrize("target,tree", [
    (folder(2), [folder(2)]),
    (folder(3, parent=2), [folder(2), folder(3, parent=2)]),
    (folder(3), [folder(2), folder(4, parent=2, for_submissions=True)]),
    (folder(3), [folder(2), folder(4, parent=2, context_id=99)]),
    (folder(3), {"error": "failed"}),
])
async def test_copy_folder_refuses_descendants_submissions_and_unverified_tree(api, target, tree):
    request, fetch = api
    request.side_effect = [folder(2), target]
    fetch.return_value = tree
    result = await (await tool("copy_course_folder"))("42", 2, target["id"])
    assert result.startswith("Error")
    assert all(call.args[0] == "get" for call in request.await_args_list)


@pytest.mark.asyncio
async def test_copy_file_always_renames_and_checks_membership(api):
    request, _ = api
    request.side_effect = [{"id": 10, "folder_id": 2}, folder(2), folder(3),
                           {"id": 11, "folder_id": 3, "display_name": "a.txt"}]
    result = await (await tool("copy_course_file"))("42", 10, 3)
    assert "file copied" in result
    assert request.await_args.kwargs["data"] == {"source_file_id": 10, "on_duplicate": "rename"}


@pytest.mark.asyncio
async def test_copy_file_refuses_submission_source(api):
    request, _ = api
    request.side_effect = [{"id": 10, "folder_id": 2}, folder(2, for_submissions=True), folder(3)]
    result = await (await tool("copy_course_file"))("42", 10, 3)
    assert result.startswith("Error")
    assert all(call.args[0] == "get" for call in request.await_args_list)
