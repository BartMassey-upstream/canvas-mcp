"""Synchronize tool signatures and role visibility without contacting Canvas."""

import argparse
import asyncio
import json
import os
from pathlib import Path


def parameter_type(schema: dict) -> str:
    alternatives = schema.get("anyOf", [schema])
    return " | ".join(part.get("type", "object") for part in alternatives)


async def render_manifest(path: Path) -> str:
    import canvas_mcp.core.config as config_module

    os.environ.update({
        "EXECUTE_TYPESCRIPT_ENABLED": "true",
        "ACCESSIBILITY_CHECKERS": "ufixit",
        "STUDENT_WRITE_TOOLS": ",".join(sorted(config_module.STUDENT_WRITE_TOOL_NAMES)),
    })
    from fastmcp import FastMCP

    from canvas_mcp import __version__
    from canvas_mcp.server import register_all_tools

    config_module._config = None
    registry = {}
    profiles = {}
    for role in ("student", "creator", "educator", "all"):
        server = FastMCP(f"manifest-{role}")
        register_all_tools(server, role=role)
        tools = {tool.name: tool for tool in await server.list_tools()}
        profiles[role] = set(tools)
        if role == "all":
            registry = tools
    manifest = json.loads(path.read_text())
    previous = {entry["name"]: entry for entry in manifest["tools"]}
    entries = []
    for name, tool in sorted(registry.items()):
        entry = previous.get(name, {
            "name": name,
            "category": (
                "student_write" if name in config_module.STUDENT_WRITE_TOOL_NAMES
                else "shared" if name in profiles["student"] & profiles["educator"]
                else "student" if name in profiles["student"] else "educator"
            ),
            "returns": "Tool result; Canvas-authored content is marked as untrusted data",
            "examples": [],
        })
        entry["description"] = tool.description or ""
        entry["roles"] = [role for role, names in profiles.items() if name in names]
        previous_parameters = {param["name"]: param for param in entry.get("parameters", [])}
        parameters = []
        for key, prop in tool.parameters.get("properties", {}).items():
            param = {
                "name": key,
                "type": parameter_type(prop),
                "required": key in tool.parameters.get("required", []),
            }
            if "default" in prop:
                param["default"] = prop["default"]
            description = prop.get("description") or previous_parameters.get(key, {}).get("description")
            if description:
                param["description"] = description
            parameters.append(param)
        entry["parameters"] = parameters
        entries.append(entry)
    manifest["version"] = __version__
    manifest["tools"] = entries
    return json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    path = Path(__file__).resolve().parents[1] / "tools" / "TOOL_MANIFEST.json"
    rendered = asyncio.run(render_manifest(path))
    if args.check:
        if path.read_text() != rendered:
            raise SystemExit("Tool manifest is stale; run uv run scripts/update_tool_manifest.py")
    else:
        path.write_text(rendered)


if __name__ == "__main__":
    main()
