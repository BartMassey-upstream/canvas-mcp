"""Synthetic persistence checks for New Quiz authoring contracts."""

from copy import deepcopy
from unittest.mock import AsyncMock, patch

import pytest
from fastmcp import FastMCP

from canvas_mcp.tools.new_quizzes import register_new_quiz_tools

A = "86c4f713-94fe-4adf-9c2c-972d68f6e739"
B = "74519fce-1eae-49e9-9d33-7d796ba932b9"
CHOICES = [
    {"id": A, "position": 1, "item_body": "<p>First</p>"},
    {"id": B, "position": 2, "item_body": "<p>Second</p>"},
]
CONTRACTS = [
    ("choice", "Equivalence", {"choices": CHOICES}, {"value": A}),
    ("multi-answer", "AllOrNothing", {"choices": CHOICES}, {"value": [A, B]}),
    (
        "true-false",
        "Equivalence",
        {"true_choice": "True", "false_choice": "False"},
        {"value": False},
    ),
    (
        "matching",
        "AllOrNothing",
        {"questions": [{"id": A, "item_body": "One"}], "answers": ["1"]},
        {
            "value": {A: "1"},
            "edit_data": {
                "matches": [
                    {"question_id": A, "question_body": "One", "answer_body": "1"}
                ],
                "distractors": [],
            },
        },
    ),
    (
        "categorization",
        "AllOrNothing",
        {
            "categories": {A: {"id": A, "item_body": "Category"}},
            "distractors": {B: {"id": B, "item_body": "Member"}},
            "category_order": [A],
        },
        {
            "value": [
                {
                    "id": A,
                    "scoring_data": {"value": [B]},
                    "scoring_algorithm": "AllOrNothing",
                }
            ],
            "score_method": "all_or_nothing",
        },
    ),
    (
        "file-upload",
        "None",
        {"files_count": "3", "restrict_count": True},
        {"value": ""},
    ),
    (
        "formula",
        "Numeric",
        {},
        {
            "value": {
                "formula": "2+y",
                "numeric": {
                    "type": "marginOfError",
                    "margin": "0",
                    "margin_type": "absolute",
                },
                "variables": [{"name": "y", "min": "1", "max": "1", "precision": 0}],
                "answer_count": "1",
                "generated_solutions": [
                    {"inputs": [{"name": "y", "value": "1"}], "output": "3"}
                ],
            }
        },
    ),
    (
        "ordering",
        "AllOrNothing",
        {
            "choices": {
                A: {"id": A, "item_body": "First"},
                B: {"id": B, "item_body": "Second"},
            }
        },
        {"value": [A, B]},
    ),
    (
        "rich-fill-blank",
        "AllOrNothing",
        {"blanks": [{"id": A, "answer_type": "openEntry"}]},
        {
            "value": [
                {
                    "id": A,
                    "scoring_data": {"value": "answer"},
                    "scoring_algorithm": "TextEquivalence",
                }
            ]
        },
    ),
    (
        "hot-spot",
        "HotSpot",
        {"image_url": "https://canvas.invalid/image.png"},
        {
            "value": {
                "type": "square",
                "coordinates": [{"x": 0.1, "y": 0.2}, {"x": 0.8, "y": 0.9}],
            }
        },
    ),
    (
        "numeric",
        "Numeric",
        {},
        {"value": [{"id": A, "type": "exactResponse", "value": "200"}]},
    ),
    (
        "essay",
        "None",
        {
            "rce": True,
            "essay": None,
            "word_count": True,
            "file_upload": False,
            "spell_check": True,
            "word_limit_enabled": False,
        },
        {"value": "Grading notes"},
    ),
]


async def tools():
    mcp = FastMCP("stage3-new-quiz")
    register_new_quiz_tools(mcp)
    return {tool.name: tool.fn for tool in await mcp.list_tools()}


@pytest.mark.asyncio
@pytest.mark.parametrize("slug,algorithm,interaction,scoring", CONTRACTS)
async def test_supported_question_contract_persists_with_identity(
    slug, algorithm, interaction, scoring
):
    saved = {}

    async def request(method, path, **kwargs):
        if method == "post":
            saved.update(deepcopy(kwargs["data"]["item"]))
            saved["id"] = 35
            return deepcopy(saved)
        assert path == "/courses/42/quizzes/12/items/35"
        return deepcopy(saved)

    with (
        patch("canvas_mcp.tools.new_quizzes.get_course_id", AsyncMock(return_value=42)),
        patch(
            "canvas_mcp.tools.new_quizzes.make_canvas_request",
            AsyncMock(side_effect=request),
        ) as mock,
    ):
        result = await (await tools())["create_new_quiz_question"](
            42,
            12,
            "<p>Synthetic stem</p>",
            slug,
            algorithm,
            scoring,
            interaction_data=interaction,
        )
    assert "New Quiz question created" in result
    assert mock.await_count == 2
    assert saved["entry"]["interaction_data"] == interaction
    assert saved["entry"]["scoring_data"] == scoring


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        None,
        [],
        {},
        {"id": None},
        {"id": True},
        {"id": "bad"},
        {"id": 99, "title": "Changed"},
    ],
)
async def test_update_quiz_invalid_response_never_reports_success(response):
    with (
        patch("canvas_mcp.tools.new_quizzes.get_course_id", AsyncMock(return_value=42)),
        patch(
            "canvas_mcp.tools.new_quizzes.make_canvas_request",
            AsyncMock(return_value=response),
        ) as request,
    ):
        result = await (await tools())["update_new_quiz"](42, 12, title="Changed")
    assert "outcome unknown" in result
    assert "New Quiz updated" not in result
    assert request.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "persisted",
    [
        {"error": "unavailable"},
        None,
        {"id": 99, "title": "Changed"},
        {"id": 12, "title": "Old"},
        {
            "id": 12,
            "title": "Changed",
            "quiz_settings": {"multiple_attempts": {"max_attempts": 2}},
        },
    ],
)
async def test_quiz_response_echo_does_not_prove_persistence(persisted):
    response = {
        "id": 12,
        "title": "Changed",
        "quiz_settings": {"multiple_attempts": {"max_attempts": 3}},
    }
    with (
        patch("canvas_mcp.tools.new_quizzes.get_course_id", AsyncMock(return_value=42)),
        patch(
            "canvas_mcp.tools.new_quizzes.make_canvas_request",
            AsyncMock(side_effect=[response, persisted]),
        ) as request,
    ):
        result = await (await tools())["update_new_quiz"](
            42,
            12,
            title="Changed",
            quiz_settings={"multiple_attempts": {"max_attempts": 3}},
        )
    assert "New Quiz updated" not in result
    assert "before retrying" in result
    assert request.await_count == 2


@pytest.mark.asyncio
async def test_partial_quiz_update_accepts_persisted_defaults_and_normalized_dates():
    persisted = {
        "id": "12",
        "due_at": "2026-10-01T00:00:00+00:00",
        "assignment_group_id": "3",
        "quiz_settings": {
            "multiple_attempts": {"max_attempts": 3, "score_to_keep": "highest"},
            "shuffle_answers": True,
        },
    }
    with (
        patch("canvas_mcp.tools.new_quizzes.get_course_id", AsyncMock(return_value=42)),
        patch(
            "canvas_mcp.tools.new_quizzes.make_canvas_request",
            AsyncMock(side_effect=[{"id": 12}, persisted]),
        ),
    ):
        result = await (await tools())["update_new_quiz"](
            42,
            12,
            due_at="2026-10-01T00:00:00Z",
            assignment_group_id=3,
            quiz_settings={"multiple_attempts": {"max_attempts": 3}},
        )
    assert "New Quiz updated" in result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation", ["uuid", "order", "missing_entry", "wrong_entry_type", "missing_points"]
)
async def test_question_echo_with_changed_persistence_never_reports_success(mutation):
    response = {
        "id": 35,
        "entry_type": "Item",
        "entry": {"interaction_data": {"choices": deepcopy(CHOICES)}},
        "points_possible": 2,
    }
    persisted = deepcopy(response)
    if mutation == "uuid":
        persisted["entry"]["interaction_data"]["choices"][0]["id"] = B
    elif mutation == "order":
        persisted["entry"]["interaction_data"]["choices"].reverse()
    elif mutation == "missing_entry":
        persisted.pop("entry")
    elif mutation == "wrong_entry_type":
        persisted["entry_type"] = "Stimulus"
    else:
        persisted.pop("points_possible")
    with (
        patch("canvas_mcp.tools.new_quizzes.get_course_id", AsyncMock(return_value=42)),
        patch(
            "canvas_mcp.tools.new_quizzes.make_canvas_request",
            AsyncMock(side_effect=[response, persisted]),
        ),
    ):
        result = await (await tools())["update_new_quiz_question"](
            42, 12, 35, interaction_data={"choices": CHOICES}, points_possible=2
        )
    assert "New Quiz question updated" not in result
    assert "Some changes may have been applied" in result


@pytest.mark.asyncio
async def test_empty_updates_issue_no_request():
    with patch(
        "canvas_mcp.tools.new_quizzes.make_canvas_request", AsyncMock()
    ) as request:
        funcs = await tools()
        assert "No New Quiz fields" in await funcs["update_new_quiz"](42, 12)
        assert "No New Quiz question fields" in await funcs["update_new_quiz_question"](
            42, 12, 35
        )
    request.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["quiz", "question"])
@pytest.mark.parametrize("response", [None, {}, {"id": True}, {"id": ""}])
async def test_create_without_recoverable_identity_requires_inspection(kind, response):
    with (
        patch("canvas_mcp.tools.new_quizzes.get_course_id", AsyncMock(return_value=42)),
        patch(
            "canvas_mcp.tools.new_quizzes.make_canvas_request",
            AsyncMock(return_value=response),
        ) as request,
    ):
        funcs = await tools()
        if kind == "quiz":
            result = await funcs["create_new_quiz"](42, "Synthetic")
        else:
            result = await funcs["create_new_quiz_question"](
                42, 12, "Synthetic stem", "essay", "None", {"value": ""}
            )
    assert "outcome unknown" in result
    assert "before retrying" in result
    assert request.await_count == 1


@pytest.mark.asyncio
async def test_question_update_wrong_response_identity_cannot_redirect_readback():
    response = {"id": 99, "entry_type": "Item", "entry": {"title": "Changed"}}
    with (
        patch("canvas_mcp.tools.new_quizzes.get_course_id", AsyncMock(return_value=42)),
        patch(
            "canvas_mcp.tools.new_quizzes.make_canvas_request",
            AsyncMock(return_value=response),
        ) as request,
    ):
        result = await (await tools())["update_new_quiz_question"](
            42, 12, 35, title="Changed"
        )
    assert "outcome unknown" in result
    assert request.await_count == 1


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"), True])
def test_nonfinite_or_boolean_points_rejected_before_write(value):
    from canvas_mcp.tools.new_quizzes import _new_quiz_payload, _question_item_payload

    assert "Invalid points_possible" in _new_quiz_payload(points_possible=value)
    assert "Invalid points_possible" in _question_item_payload(points_possible=value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation",
    [
        "create_new_quiz",
        "update_new_quiz",
        "create_new_quiz_question",
        "update_new_quiz_question",
    ],
)
@pytest.mark.parametrize(
    "outcome", ["may_have_written", "rejected", "not_dispatched", "untyped"]
)
async def test_definition_failures_preserve_write_uncertainty(operation, outcome):
    from canvas_mcp.core.write_outcome import RequestFailure, WriteOutcome

    failure = (
        {"error": "Sensitive remote detail"}
        if outcome == "untyped"
        else RequestFailure("Sensitive remote detail", WriteOutcome(outcome))
    )
    args = {
        "create_new_quiz": (42, "Synthetic"),
        "update_new_quiz": (42, 12),
        "create_new_quiz_question": (42, 12, "Stem", "essay", "None", {"value": ""}),
        "update_new_quiz_question": (42, 12, 35),
    }
    kwargs = {"title": "Changed"} if operation.startswith("update") else {}
    with (
        patch("canvas_mcp.tools.new_quizzes.get_course_id", AsyncMock(return_value=42)),
        patch(
            "canvas_mcp.tools.new_quizzes.make_canvas_request",
            AsyncMock(return_value=failure),
        ) as request,
    ):
        result = await (await tools())[operation](*args[operation], **kwargs)
    assert result.startswith("Error:")
    assert "Sensitive remote detail" not in result
    assert ("outcome unknown" in result) == (outcome in {"may_have_written", "untyped"})
    assert request.await_count == 1
