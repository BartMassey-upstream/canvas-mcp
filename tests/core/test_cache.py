"""Tests for resolving human-facing Canvas course identifiers."""

from unittest.mock import AsyncMock, patch

import pytest

import canvas_mcp.core.cache as cache


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
