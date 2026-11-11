"""Registered-client regressions for exact page and assignment edits."""

import copy
import hashlib
from unittest.mock import AsyncMock

import pytest
from fastmcp import Client, FastMCP

from canvas_mcp.tools import assignments, pages

BODY = '<p><a href="/courses/42/files/7">file</a></p><pre>a  b\n c</pre>'
STAMP = '2026-11-10T12:30:00.123456Z'


@pytest.fixture(params=['page', 'assignment'])
def api(request, monkeypatch):
    kind = request.param
    module = pages if kind == 'page' else assignments
    state = {'page_id': 7, 'id': 7, 'url': 'old', 'body': BODY,
             'description': BODY, 'title': 'Old', 'name': 'Old',
             'updated_at': STAMP, 'due_at': '2026-11-01T00:00:00Z',
             'submission_types': ['external_tool']}
    behavior = {'readback': None, 'write': None}
    calls = []

    async def request_api(method, path, **kwargs):
        calls.append((method, path, copy.deepcopy(kwargs)))
        if method == 'put':
            if behavior['write'] is not None:
                return behavior['write']
            state.update(kwargs['data']['wiki_page' if kind == 'page' else 'assignment'])
            if state['title'] == 'Renamed':
                state['url'] = 'renamed'
            return dict(state)
        if any(call[0] == 'put' for call in calls) and behavior['readback'] is not None:
            return behavior['readback'](dict(state))
        return dict(state)

    monkeypatch.setattr(module, 'get_course_id', AsyncMock(return_value='42'))
    monkeypatch.setattr(module, 'get_course_code', AsyncMock(return_value='CS42'))
    monkeypatch.setattr(module, 'make_canvas_request', request_api)
    server = FastMCP('general-edits')
    if kind == 'page':
        pages.register_educator_page_crud_tools(server)
    else:
        assignments.register_educator_assignment_tools(server)
        assignments.register_shared_assignment_tools(server)

    async def edit(**kwargs):
        body = kwargs.pop('body', None)
        if body is not None:
            kwargs['new_content' if kind == 'page' else 'description'] = body
        args = {'course_identifier': '42', 'page_url_or_id' if kind == 'page' else 'assignment_id': 'old' if kind == 'page' else 7, **kwargs}
        async with Client(server) as client:
            result = await client.call_tool('edit_page_content' if kind == 'page' else 'update_assignment', args)
        return '\n'.join(item.text for item in result.content if hasattr(item, 'text'))

    return kind, state, behavior, calls, edit, server


@pytest.mark.asyncio
@pytest.mark.parametrize('guard', [
    {'expect_updated_at': '2026-11-10T12:30:00.123455Z'},
    {'expect_body_sha256': '0' * 64},
    {'find': 'absent', 'replace': 'new'},
    {'find': 'a', 'replace': 'new'},
    {'require': ['missing']},
])
async def test_failed_preconditions_never_write(api, guard):
    _, _, _, calls, edit, _ = api
    await edit(**guard, **({} if 'find' in guard else {'body': '<p>new</p>'}))
    assert not any(c[0] == 'put' for c in calls)


@pytest.mark.asyncio
async def test_timestamp_timezone_precision_and_same_second_success(api):
    _, state, _, calls, edit, _ = api
    result = await edit(find='a  b', replace='a   b',
                        expect_updated_at='2026-11-10T04:30:00.123456-08:00',
                        expect_body_sha256=hashlib.sha256(BODY.encode()).hexdigest())
    assert state['body' if api[0] == 'page' else 'description'] == BODY.replace('a  b', 'a   b')
    assert [c[0] for c in calls] == ['get', 'put', 'get']
    assert 'success' in result.lower() or 'confirmed by reading' in result.lower()


@pytest.mark.asyncio
@pytest.mark.parametrize('changed', ['link', 'whitespace'])
async def test_exact_body_readback_required_without_guards(api, changed):
    _, _, behavior, calls, edit, _ = api
    field = 'body' if api[0] == 'page' else 'description'
    def normalize(state):
        state[field] = BODY.replace('/files/7', '/files/8') if changed == 'link' else BODY.replace('a  b', 'a b')
        return state
    behavior['readback'] = normalize
    result = await edit(body=BODY)
    assert [c[0] for c in calls] == ['get', 'put', 'get']
    assert 'success' not in result.lower()


@pytest.mark.asyncio
@pytest.mark.parametrize('snapshot', [[], {'id': 7, 'page_id': 7}, {'id': True, 'page_id': True, 'body': BODY, 'description': BODY}])
async def test_malformed_preflight_never_writes(api, snapshot, monkeypatch):
    kind, _, _, calls, edit, _ = api
    module = pages if kind == 'page' else assignments
    request = AsyncMock(return_value=snapshot)
    monkeypatch.setattr(module, 'make_canvas_request', request)
    await edit(body=BODY)
    assert all(c.args[0] != 'put' for c in request.await_args_list)
    assert calls == []


@pytest.mark.asyncio
async def test_title_only_verifies_identity_and_requested_field(api):
    kind, _, behavior, calls, edit, _ = api
    field = 'title' if kind == 'page' else 'name'
    behavior['readback'] = lambda state: {**state, field: 'Ignored'}
    result = await edit(**{field: 'Renamed'})
    assert 'success' not in result.lower()
    assert len(calls) == 3
    if kind == 'page':
        assert calls[1][1].endswith('/page_id:7')
        assert calls[2][1].endswith('/renamed')


@pytest.mark.asyncio
async def test_wrong_identity_on_readback_is_unconfirmed(api):
    _, _, behavior, _, edit, _ = api
    behavior['readback'] = lambda state: {**state, 'id': 8, 'page_id': 8}
    result = await edit(body=BODY)
    assert 'success' not in result.lower()


@pytest.mark.asyncio
async def test_explicit_identity_mismatch_never_writes(api):
    kind, _, _, calls, edit, _ = api
    args = {'page_url_or_id': 'page_id:8'} if kind == 'page' else {'assignment_id': 8}
    result = await edit(body=BODY, **args)
    assert 'Nothing was written' in result
    assert not any(c[0] == 'put' for c in calls)


@pytest.mark.asyncio
async def test_noop_body_edit_is_verified(api):
    _, _, _, calls, edit, _ = api
    result = await edit(body=BODY)
    assert len(calls) == 3
    assert 'success' in result.lower() or 'confirmed by reading' in result.lower()


@pytest.mark.asyncio
async def test_assignment_null_and_nested_metadata_require_readback(api):
    kind, _, behavior, calls, edit, _ = api
    if kind != 'assignment':
        pytest.skip('assignment options')
    behavior['readback'] = lambda state: {**state, 'due_at': 'old', 'external_tool_tag_attributes': {'url': 'https://wrong.example'}}
    result = await edit(clear_due_at=True, external_tool_url='https://tool.example/launch')
    assert 'success' not in result.lower()
    payload = next(c for c in calls if c[0] == 'put')[2]['data']['assignment']
    assert payload['due_at'] is None
    assert payload['external_tool_tag_attributes']['url'] == 'https://tool.example/launch'


@pytest.mark.asyncio
async def test_assignment_read_exposes_exact_metadata(api):
    kind, _, _, _, _, server = api
    if kind != 'assignment':
        pytest.skip('assignment read')
    async with Client(server) as client:
        result = await client.call_tool('get_assignment_details', {'course_identifier': '42', 'assignment_id': 7})
    text = '\n'.join(item.text for item in result.content if hasattr(item, 'text'))
    assert STAMP in text
    assert hashlib.sha256(BODY.encode()).hexdigest() in text


@pytest.mark.asyncio
@pytest.mark.parametrize('kwargs', [
    {'body': BODY, 'find': 'a  b', 'replace': 'x'},
    {'find': 'a  b'},
    {'replace': 'x'},
    {'find': '', 'replace': 'x'},
    {'body': BODY, 'expect_body_sha256': 'invalid'},
    {'find': 'a  b', 'replace': '<<<UNTRUSTED CANVAS CONTENT (x): text>>>'},
])
async def test_invalid_edit_specification_never_writes(api, kwargs):
    _, _, _, calls, edit, _ = api
    await edit(**kwargs)
    assert not any(c[0] == 'put' for c in calls)


@pytest.mark.asyncio
async def test_metadata_only_does_not_need_missing_body(api):
    kind, state, _, calls, edit, _ = api
    state.pop('body')
    state.pop('description')
    result = await edit(**{'title' if kind == 'page' else 'name': 'New'})
    assert 'confirmed by reading' in result.lower()
    payload = calls[1][2]['data']['wiki_page' if kind == 'page' else 'assignment']
    assert 'body' not in payload and 'description' not in payload


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', [{'error': 'bad write'}, None])
async def test_failed_or_missing_readback_never_reports_success(api, failure):
    _, _, behavior, calls, edit, _ = api
    behavior['readback'] = lambda state: failure
    result = await edit(body=BODY)
    assert 'confirmed by reading' not in result.lower()
    assert 'unconfirmed' in result.lower()
    assert [c[0] for c in calls] == ['get', 'put', 'get']


@pytest.mark.asyncio
async def test_numeric_returned_slug_uses_page_id_for_readback(api):
    kind, state, _, calls, edit, _ = api
    if kind != 'page':
        pytest.skip('page slug')
    state['url'] = '999'
    result = await edit(body=BODY)
    assert 'confirmed by reading' in result.lower()
    assert calls[2][1].endswith('/page_id:7')
