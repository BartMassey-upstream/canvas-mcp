"""Run the installed Canvas package over stdio using shared synthetic data."""

import argparse
import asyncio
import json
import os
import sys
from contextlib import redirect_stdout
from pathlib import Path

import httpx
from fastmcp import FastMCP
from synthetic_canvas import SyntheticCanvas


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--actor", choices=["instructor", "restricted", "student"], default="instructor"
    )
    parser.add_argument(
        "--role", choices=["educator", "creator", "student"], default="educator"
    )
    parser.add_argument("--audit-json", type=Path)
    args = parser.parse_args()
    os.environ.update(
        CANVAS_API_URL="https://canvas.invalid/api/v1",
        CANVAS_API_TOKEN="stage3-dummy-token",
        CANVAS_ROLE=args.role,
        ENABLE_DATA_ANONYMIZATION="false",
        STUDENT_WRITE_TOOLS="",
        EXECUTE_TYPESCRIPT_ENABLED="false",
    )
    from canvas_mcp.core import client as canvas_client
    from canvas_mcp.core import config as canvas_config
    from canvas_mcp.core.tool_policy import apply_tool_policy, resolve_tool_policy
    from canvas_mcp.server import register_all_tools

    canvas_config._open_canvas_token_file = lambda: None
    canvas_config.reset_config()
    canvas = SyntheticCanvas(args.actor)
    http = httpx.AsyncClient(transport=httpx.MockTransport(canvas.respond))
    canvas_client._get_http_client = lambda: http
    server = FastMCP("installed-wheel-synthetic-workflow")
    with redirect_stdout(sys.stderr):
        register_all_tools(server, role=args.role)

    @server.tool()
    def synthetic_audit() -> dict:
        """Return synthetic transport evidence without touching Canvas."""
        import canvas_mcp

        return {
            "requests": canvas.requests,
            "writes": canvas.writes,
            "package_path": canvas_mcp.__file__,
            "environment_prefix": sys.prefix,
        }

    asyncio.run(apply_tool_policy(server, resolve_tool_policy(None, "stdio")))
    try:
        server.run(transport="stdio", show_banner=False)
    finally:
        asyncio.run(http.aclose())
        if args.audit_json:
            import canvas_mcp

            args.audit_json.write_text(
                json.dumps(
                    {
                        "requests": canvas.requests,
                        "writes": canvas.writes,
                        "package_path": canvas_mcp.__file__,
                        "environment_prefix": sys.prefix,
                    },
                    indent=2,
                )
                + "\n"
            )


if __name__ == "__main__":
    main()
