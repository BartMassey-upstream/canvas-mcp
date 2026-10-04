"""Exercise shared synthetic workflows with a chosen installed interpreter."""

import argparse
import asyncio
import json
import os
import tempfile
from pathlib import Path

from fastmcp import Client
from fastmcp.client.transports import StdioTransport


def text(result):
    assert not result.is_error
    return "\n".join(block.text for block in result.content if hasattr(block, "text"))


def validate_installed_package(audit, python):
    environment = python.absolute().parent.parent.resolve()
    package = Path(audit["package_path"]).resolve()
    assert Path(audit["environment_prefix"]).resolve() == environment, (
        "Wrong interpreter environment"
    )
    assert package.is_relative_to(environment), (
        "Package imported outside chosen environment"
    )
    assert "site-packages" in package.relative_to(environment).parts, (
        "Package imported from source checkout"
    )


async def run(python, output):
    evidence = []
    child_env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"PYTHONPATH", "PYTHONHOME"}
    }
    for actor, role in [
        ("instructor", "educator"),
        ("restricted", "educator"),
        ("student", "student"),
        ("instructor", "creator"),
    ]:
        transport = StdioTransport(
            command=str(python),
            args=[
                str(Path(__file__).with_name("mock_stdio_server.py")),
                "--actor",
                actor,
                "--role",
                role,
            ],
            env={
                **child_env,
                "CANVAS_API_URL": "https://canvas.invalid/api/v1",
                "CANVAS_API_TOKEN": "stage3-dummy-token",
                "ALLOWED_WRITE_TOOLS": "all",
            },
            cwd=str(output.parent),
            keep_alive=False,
        )
        async with Client(transport) as mcp:
            names = {tool.name for tool in await mcp.list_tools()}
            assert "execute_typescript" not in names
            identity = text(await mcp.call_tool("get_my_enrollments", {}))
            assert (
                "StudentEnrollment" if actor == "student" else "TeacherEnrollment"
            ) in identity
            calls = ["get_my_enrollments"]
            snapshot_tools = {
                "capture_record_snapshot",
                "verify_record_snapshot",
                "compare_record_snapshots",
                "lookup_student_identities",
            }
            if role == "educator":
                assert snapshot_tools <= names
            else:
                assert not snapshot_tools & names
            if role == "creator":
                assert (
                    not {
                        "get_assignment_analytics",
                        "send_conversation",
                        "list_submissions",
                    }
                    & names
                )
            elif actor == "instructor":
                analytics = text(
                    await mcp.call_tool(
                        "get_assignment_analytics",
                        {
                            "course_identifier": "SYNTH/42",
                            "assignment_id": 34,
                        },
                    )
                )
                assert "Submitted: 2/4" in analytics and "Missing: 1/4" in analytics
                assert "Graded: 1/4" in analytics and "Late: 1/2" in analytics
                draft = json.loads(
                    text(
                        await mcp.call_tool(
                            "send_conversation",
                            {
                                "course_identifier": 42,
                                "recipient_ids": ["101"],
                                "subject": "Followup",
                                "body": "Please review your practice status.",
                            },
                        )
                    )
                )
                assert draft["nothing_sent"] and draft["preview"]
                calls += ["get_assignment_analytics", "send_conversation:preview"]
                with tempfile.TemporaryDirectory(
                    prefix="synthetic-snapshots-", dir=output.parent
                ) as private:
                    arguments = {
                        "course_identifier": 42,
                        "save_directory": str(Path(private) / "captures"),
                        "families": ["course", "assignments"],
                        "page_budget": 10,
                    }
                    captures = []
                    for _ in range(2):
                        preview = json.loads(
                            text(
                                await mcp.call_tool(
                                    "capture_record_snapshot", arguments
                                )
                            )
                        )
                        assert preview["preview"] and preview["nothing_stored"]
                        captured = json.loads(
                            text(
                                await mcp.call_tool(
                                    "capture_record_snapshot",
                                    {
                                        **arguments,
                                        "confirmation_token": preview[
                                            "confirmation_token"
                                        ],
                                    },
                                )
                            )
                        )
                        assert (
                            captured["state"] == "complete"
                            and captured["canvas_writes"] == 0
                        )
                        verified = json.loads(
                            text(
                                await mcp.call_tool(
                                    "verify_record_snapshot",
                                    {
                                        "snapshot_directory": captured[
                                            "snapshot_directory"
                                        ],
                                    },
                                )
                            )
                        )
                        assert (
                            verified["status"] == "verified"
                            and verified["families"]["assignments"]["records"] == 2
                        )
                        captures.append(captured["snapshot_directory"])
                    compared = json.loads(
                        text(
                            await mcp.call_tool(
                                "compare_record_snapshots",
                                {
                                    "before_directory": captures[0],
                                    "after_directory": captures[1],
                                },
                            )
                        )
                    )
                    assert compared["status"] == "compared"
                    assert all(
                        item["coverage_complete"]
                        and item["added"] == item["removed"] == item["changed"] == 0
                        for item in compared["families"].values()
                    )
                calls += [
                    "capture_record_snapshot:preview",
                    "capture_record_snapshot:confirmed-local-write",
                    "verify_record_snapshot",
                    "compare_record_snapshots",
                ]
            elif actor == "restricted":
                result = await mcp.call_tool(
                    "get_assignment_analytics",
                    {
                        "course_identifier": 42,
                        "assignment_id": 34,
                    },
                    raise_on_error=False,
                )
                assert (
                    result.is_error
                    and "fetching students" in result.content[0].text
                )
                calls += ["get_assignment_analytics:rejected"]
            else:
                assert "submit_assignment" not in names
                status = text(
                    await mcp.call_tool(
                        "get_my_submission_status", {"course_identifier": 42}
                    )
                )
                assert (
                    "Missing Submissions (1)" in status
                    and "External-tool assignments (1)" in status
                )
                calls += ["get_my_submission_status"]
            audit = json.loads(text(await mcp.call_tool("synthetic_audit", {})))
            assert audit["writes"] == []
            validate_installed_package(audit, python)
            evidence.append(
                {
                    "actor": actor,
                    "profile": role,
                    "calls": calls,
                    "result": "passed",
                    **audit,
                }
            )
    output.write_text(json.dumps(evidence, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    asyncio.run(run(args.python.absolute(), args.output.resolve()))


if __name__ == "__main__":
    main()
