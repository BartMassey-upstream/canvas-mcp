"""Tests for resolving human-facing Canvas course identifiers."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

import canvas_mcp.core.cache as cache
from canvas_mcp.core.credentials import (
    RequestCredentials,
    clear_http_request_context,
    get_request_credentials,
    set_http_request_active,
    set_request_credentials,
)


@pytest.fixture(autouse=True)
def clear_course_cache():
    cache.course_code_to_id_cache.clear()
    cache.id_to_course_code_cache.clear()
    yield
    cache.course_code_to_id_cache.clear()
    cache.id_to_course_code_cache.clear()


@pytest.mark.asyncio
async def test_course_code_with_arbitrary_punctuation_is_resolved():
    async def refresh():
        cache.course_code_to_id_cache["CS-423/523-001 Fall 2026"] = "117989"
        cache.id_to_course_code_cache["117989"] = "CS-423/523-001 Fall 2026"
        return True

    with patch.object(
        cache, "refresh_course_cache", side_effect=refresh
    ) as refresh_mock:
        assert await cache.get_course_id("CS-423/523-001 Fall 2026") == "117989"

    refresh_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_refresh_preserves_cache_objects_imported_by_tool_modules():
    code_cache = cache.course_code_to_id_cache
    id_cache = cache.id_to_course_code_cache
    courses = [{"id": 117989, "course_code": "CS-423/523-001 Fall 2026"}]

    with patch.object(
        cache, "fetch_all_paginated_results", new=AsyncMock(return_value=courses)
    ):
        assert await cache.refresh_course_cache() is True

    assert cache.course_code_to_id_cache is code_cache
    assert cache.id_to_course_code_cache is id_cache
    assert code_cache["CS-423/523-001 Fall 2026"] == "117989"


@pytest.mark.asyncio
async def test_unresolved_course_code_fails_closed():
    with patch.object(cache, "refresh_course_cache", new=AsyncMock(return_value=True)):
        with pytest.raises(ValueError, match="Could not resolve course code"):
            await cache.get_course_id("not/a course?#")


@pytest.mark.asyncio
async def test_explicit_sis_course_id_does_not_need_cache_lookup():
    with patch.object(cache, "refresh_course_cache", new_callable=AsyncMock) as refresh:
        assert await cache.get_course_id("sis_course_id:CS/101.11é") == (
            "sis_course_id:CS/101.11é"
        )

    refresh.assert_not_awaited()


@pytest.mark.asyncio
async def test_empty_sis_course_id_is_rejected():
    with pytest.raises(ValueError, match="must include an ID"):
        await cache.get_course_id("sis_course_id:")


@pytest.mark.asyncio
async def test_duplicate_code_refuses_target_and_labels_cannot_remove_collision():
    courses = [{"id": 111, "course_code": "SAME"},
               {"id": 222, "course_code": "SAME"}]
    with patch.object(cache, "fetch_all_paginated_results", new=AsyncMock(return_value=courses)):
        assert await cache.refresh_course_cache()
        cache.remember_course_labels([courses[0]])
        with pytest.raises(ValueError, match="Ambiguous course code"):
            await cache.get_course_id("SAME")
        assert await cache.get_course_id(111) == "111"
        assert await cache.get_course_id("sis_course_id:chosen") == "sis_course_id:chosen"


@pytest.mark.asyncio
async def test_individual_course_read_cannot_establish_alias_uniqueness():
    with patch.object(cache, "fetch_all_paginated_results", new=AsyncMock(return_value=[])), patch.object(
        cache, "make_canvas_request", new=AsyncMock(return_value={"id": 111, "course_code": "SAME"})
    ):
        assert await cache.get_course_code(111) == "SAME"
        assert "SAME" not in cache.course_code_to_id_cache
        with pytest.raises(ValueError, match="Could not resolve course code"):
            await cache.get_course_id("SAME")


@pytest.mark.asyncio
async def test_concurrent_http_callers_resolve_same_alias_independently():
    cache.course_code_to_id_cache["SAME"] = "999"
    cache.id_to_course_code_cache["999"] = "PRIVATE"
    async def fetch(*args, **kwargs):
        credential = get_request_credentials()
        await asyncio.sleep(0)
        return [{"id": int(credential.api_token), "course_code": "SAME"}]

    async def caller(course_id):
        set_http_request_active()
        set_request_credentials(RequestCredentials(str(course_id), "https://canvas.invalid/api/v1"))
        try:
            result = await cache.get_course_id("SAME")
            assert await cache.get_course_code(course_id) == "SAME"
            cache.remember_course_labels([{"id": course_id, "course_code": "LOCAL"}])
            assert await cache.get_course_code(course_id) == "LOCAL"
            return result
        finally:
            clear_http_request_context()

    with patch.object(cache, "fetch_all_paginated_results", side_effect=fetch):
        assert await asyncio.gather(caller(111), caller(222)) == ["111", "222"]
    assert cache.course_code_to_id_cache == {"SAME": "999"}
    assert cache.id_to_course_code_cache == {"999": "PRIVATE"}


@pytest.mark.asyncio
async def test_http_label_cannot_read_stdio_cache_and_credentials_reset_request_state():
    cache.id_to_course_code_cache["111"] = "PRIVATE"
    set_http_request_active()
    set_request_credentials(RequestCredentials("one", "https://canvas.invalid/api/v1"))
    try:
        with patch.object(cache, "fetch_all_paginated_results", new=AsyncMock(return_value=[])), patch.object(
            cache, "make_canvas_request", new=AsyncMock(return_value={"error": "HTTP error: 403"})
        ) as request:
            assert await cache.get_course_code(111) == "111"
            request.assert_awaited_once()
            cache.remember_course_labels([{"id": 111, "course_code": "FIRST"}])
            assert await cache.get_course_code(111) == "FIRST"
            set_request_credentials(RequestCredentials("two", "https://canvas.invalid/api/v1"))
            assert await cache.get_course_code(111) == "111"
    finally:
        clear_http_request_context()


def test_credential_repr_hides_token_but_auth_header_keeps_it():
    from canvas_mcp.core.client import _canvas_auth_headers
    credential = RequestCredentials("synthetic-secret", "https://canvas.invalid/api/v1")
    assert "synthetic-secret" not in repr(credential)
    assert _canvas_auth_headers(credential.api_token)["Authorization"] == "Bearer synthetic-secret"
