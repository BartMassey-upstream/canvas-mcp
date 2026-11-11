"""Registered accessibility workflows reject incomplete data and verify writes."""
import json
from unittest.mock import AsyncMock, patch

import pytest
from fastmcp import Client, FastMCP

from canvas_mcp.tools.accessibility import register_accessibility_tools


@pytest.fixture
def api():
    with patch('canvas_mcp.tools.accessibility.get_course_id', new_callable=AsyncMock, return_value=42), \
         patch('canvas_mcp.tools.accessibility.fetch_all_paginated_results', new_callable=AsyncMock) as listing, \
         patch('canvas_mcp.tools.accessibility.make_canvas_request', new_callable=AsyncMock) as request:
        listing.return_value = [{"page_id": 1, "url": "home", "title": "Title", "body": "<th>A</th>"}]
        request.return_value = {"body": "<th>A</th>", "url": "home", "page_id": 1}
        yield listing, request


async def call(name, **kwargs):
    mcp = FastMCP('test')
    register_accessibility_tools(mcp)
    async with Client(mcp) as client:
        result = await client.call_tool_mcp(name, {"course_identifier": 42, **kwargs})
    return json.loads(result.content[0].text)


@pytest.mark.asyncio
@pytest.mark.parametrize('body,valid', [(None, True), ('missing', False), (123, False)])
async def test_scan_body_validation(api, body, valid):
    listing, _ = api
    row = listing.return_value[0]
    if body == 'missing':
        del row['body']
    else:
        row['body'] = body
    result = await call('scan_course_content_accessibility', content_types='pages')
    assert ('summary' in result) is valid
    assert listing.call_args.args[1]['include[]'] == 'body'


@pytest.mark.asyncio
@pytest.mark.parametrize('types', ['', 'discussions', 'syllabus', 'pages,unknown'])
async def test_unsupported_types_fail_before_network(api, types):
    listing, request = api
    for name in ('scan_course_content_accessibility', 'fix_accessibility_issues'):
        assert 'error' in await call(name, content_types=types)
    listing.assert_not_called()
    request.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('listing_value', [{'error': 'secret'}, [None], [{'url': 'x', 'title': 'x', 'body': ''}]])
async def test_invalid_listing_aborts(api, listing_value):
    listing, request = api
    listing.return_value = listing_value
    assert 'error' in await call('fix_accessibility_issues', dry_run=False)
    request.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('detail', [{'error': 'private'}, {}, {'body': 12}])
async def test_invalid_detail_aborts_without_writes(api, detail):
    _, request = api
    request.return_value = detail
    assert 'error' in await call('fix_accessibility_issues', dry_run=False)
    assert all(c.args[0] != 'put' for c in request.call_args_list)


@pytest.mark.asyncio
async def test_dry_run_and_verified_write(api):
    _, request = api
    preview = await call('fix_accessibility_issues')
    assert not preview['results'][0]['applied']
    assert all(c.args[0] == 'get' for c in request.call_args_list)
    request.reset_mock()
    request.side_effect = [{'body': '<th>A</th>', 'url': 'home', 'page_id': 1}, {'body': '<th>A</th>', 'url': 'home', 'page_id': 1}, {},
                           {'body': '<th scope="col">A</th>', 'url': 'home', 'page_id': 1}]
    result = await call('fix_accessibility_issues', dry_run=False)
    assert result['results'][0]['applied']
    put = request.call_args_list[2]
    assert 'UNTRUSTED CANVAS CONTENT' not in str(put.kwargs['data'])
    assert 'UNTRUSTED CANVAS CONTENT' in result['results'][0]['title']


@pytest.mark.asyncio
@pytest.mark.parametrize('responses,expected_put', [
    ([{'body': '<th>A</th>', 'url': 'home', 'page_id': 1}, {'body': 'changed', 'url': 'home', 'page_id': 1}], False),
    ([{'body': '<th>A</th>', 'url': 'home', 'page_id': 1}, {'body': '<th>A</th>', 'url': 'home', 'page_id': 1}, {'error': 'secret'}], True),
    ([{'body': '<th>A</th>', 'url': 'home', 'page_id': 1}, {'body': '<th>A</th>', 'url': 'home', 'page_id': 1}, {}, {'body': 'changed', 'url': 'home', 'page_id': 1}], True),
    ([{'body': '<th>A</th>', 'url': 'home', 'page_id': 1}, {'body': '<th>A</th>', 'url': 'home', 'page_id': 1}, {}, {'error': 'secret'}], True),
])
async def test_unconfirmed_writes(api, responses, expected_put):
    _, request = api
    request.side_effect = responses
    result = await call('fix_accessibility_issues', dry_run=False)
    assert not result['results'][0]['applied']
    assert 'error' in result['results'][0]
    assert 'secret' not in json.dumps(result)
    assert any(c.args[0] == 'put' for c in request.call_args_list) is expected_put


@pytest.mark.asyncio
async def test_invalid_fix_type_and_mismatched_detail(api):
    listing, request = api
    assert 'error' in await call('fix_accessibility_issues', fix_types='typo')
    listing.assert_not_called()
    request.return_value = {'body': '<th>A</th>', 'url': 'other', 'page_id': 1}
    assert 'error' in await call('fix_accessibility_issues', dry_run=False)
    assert all(c.args[0] == 'get' for c in request.call_args_list)


@pytest.mark.asyncio
async def test_all_sources_validated_before_writes(api):
    listing, request = api
    listing.side_effect = [listing.return_value,
                           [{'id': 2, 'name': 'Assignment', 'description': '<th>B</th>'}]]
    request.side_effect = [{'body': '<th>A</th>', 'url': 'home', 'page_id': 1},
                           {'id': 2}]
    result = await call('fix_accessibility_issues', content_types='pages,assignments', dry_run=False)
    assert 'error' in result
    assert all(c.args[0] == 'get' for c in request.call_args_list)


@pytest.mark.asyncio
@pytest.mark.parametrize("readback", [
    {"body": '<th scope="col">A</th>'},
    {"body": '<th scope="col">A</th>', "url": "other", "page_id": 1},
])
async def test_readback_requires_matching_identity(api, readback):
    _, request = api
    source = {"body": "<th>A</th>", "url": "home", "page_id": 1}
    request.side_effect = [source, source, {}, readback]
    result = await call('fix_accessibility_issues', dry_run=False)
    assert not result['results'][0]['applied']
    assert 'unconfirmed' in result['results'][0]['error']


@pytest.mark.asyncio
async def test_prewrite_identity_failure_does_not_write(api):
    _, request = api
    request.side_effect = [{"body": "<th>A</th>", "url": "home", "page_id": 1},
                           {"body": "<th>A</th>"}]
    result = await call('fix_accessibility_issues', dry_run=False)
    assert 'no write attempted' in result['results'][0]['error']
    assert all(c.args[0] == 'get' for c in request.call_args_list)


@pytest.mark.asyncio
async def test_assignment_write_and_readback(api):
    listing, request = api
    listing.return_value = [{"id": 10, "name": "Assignment", "description": "<th>A</th>"}]
    source = {"id": 10, "description": "<th>A</th>"}
    request.side_effect = [source, source, {},
                           {"id": 10, "description": '<th scope="col">A</th>'}]
    result = await call('fix_accessibility_issues', content_types='assignments', dry_run=False)
    assert result['results'][0]['applied']
    assert request.call_args_list[2].args[1] == '/courses/42/assignments/10'


@pytest.mark.asyncio
async def test_validation_errors_are_actionable_and_safe(api):
    listing, _ = api
    assert 'content_types' in (await call('scan_course_content_accessibility', content_types='unknown'))['error']
    assert 'fix type' in (await call('fix_accessibility_issues', fix_types='wrong'))['error']
    listing.return_value = [{'error': 'secret'}]
    result = await call('scan_course_content_accessibility', content_types='pages')
    assert 'listing' in result['error']
    assert 'secret' not in json.dumps(result)
