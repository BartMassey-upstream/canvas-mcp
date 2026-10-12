"""Central MCP wire-result behavior for Canvas tools (issues 270 and 271)."""

import inspect
import json
from collections.abc import Callable
from typing import Any, get_type_hints

import mcp.types as mt
from fastmcp import FastMCP
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools import ToolResult

_INSTALL_ATTR = "_canvas_tool_result_contract_installed"

#: Client metadata requesting room for complete Canvas content results.
MAX_RESULT_SIZE_CHARS = 500_000
FULL_CONTENT_TOOL_META: dict[str, Any] = {
    "anthropic/maxResultSizeChars": MAX_RESULT_SIZE_CHARS,
}

_FULL_CONTENT_TOOLS = frozenset({
    "get_page_content", "get_syllabus", "get_front_page",
    "get_assignment_details", "get_discussion_topic_details",
    "get_discussion_entry_details", "get_discussion_with_replies",
    "list_discussion_entries", "get_conversation_details",
    "get_my_submission", "get_rubric", "get_rubric_assessment",
    "get_announcement",
})


def _text_is_error(text: str) -> bool:
    candidate = text.lstrip()
    if candidate.startswith(("Error", "❌")):
        return True
    try:
        parsed = json.loads(candidate)
    except (json.JSONDecodeError, TypeError):
        return False
    return isinstance(parsed, dict) and "error" in parsed


def _result_is_error(result: ToolResult) -> bool:
    structured = result.structured_content
    if isinstance(structured, dict):
        if "error" in structured:
            return True
        if set(structured) == {"result"}:
            wrapped = structured["result"]
            if isinstance(wrapped, str) and _text_is_error(wrapped):
                return True

    for block in result.content:
        if isinstance(block, mt.TextContent) and _text_is_error(block.text):
            return True
    return False


class CanvasToolResultMiddleware(Middleware):
    """Map established Canvas failure payloads to MCP isError."""

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        result = await call_next(context)
        if context.message.name == "bulk_grade_submissions" and result.structured_content is None:
            for block in result.content:
                if isinstance(block, mt.TextContent) and "\nRecovery data (JSON):\n" in block.text:
                    try:
                        recovery = json.loads(block.text.rsplit("\nRecovery data (JSON):\n", 1)[1])
                    except (ValueError, TypeError):
                        continue
                    if (
                        isinstance(recovery, dict)
                        and recovery.get("schema_version") == 1
                        and isinstance(recovery.get("items"), list)
                        and isinstance(recovery.get("counts"), dict)
                        and type(recovery.get("dry_run")) is bool
                    ):
                        result.structured_content = recovery
        if not result.is_error and _result_is_error(result):
            result.is_error = True
        return result


def _returns_str(fn: Callable[..., Any]) -> bool:
    try:
        annotation = get_type_hints(fn).get("return", inspect.Signature.empty)
    except (NameError, TypeError):
        annotation = inspect.signature(fn).return_annotation
    return annotation is str


def _install_tool_decorator_wrapper(mcp: FastMCP) -> None:
    original_tool = mcp.tool

    def registration_options(fn: Callable[..., Any], options: dict[str, Any]) -> dict[str, Any]:
        if "output_schema" not in options and _returns_str(fn):
            options["output_schema"] = None
        if fn.__name__ in _FULL_CONTENT_TOOLS:
            options["meta"] = {**FULL_CONTENT_TOOL_META, **(options.get("meta") or {})}
        return options

    def canvas_tool(name_or_fn: Any = None, **kwargs: Any) -> Any:
        if callable(name_or_fn):
            options = registration_options(name_or_fn, dict(kwargs))
            return original_tool(name_or_fn, **options)

        def register(fn: Callable[..., Any]) -> Any:
            options = registration_options(fn, dict(kwargs))
            decorator = original_tool(name_or_fn, **options)
            return decorator(fn)

        return register

    setattr(mcp, "tool", canvas_tool)  # noqa: B010


def install_tool_result_contract(mcp: FastMCP) -> None:
    """Install Canvas result behavior once on one FastMCP server."""
    if getattr(mcp, _INSTALL_ATTR, False):
        return
    _install_tool_decorator_wrapper(mcp)
    mcp.add_middleware(CanvasToolResultMiddleware())
    setattr(mcp, _INSTALL_ATTR, True)
