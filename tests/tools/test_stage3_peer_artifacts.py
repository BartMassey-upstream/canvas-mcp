import json
import stat
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastmcp import FastMCP

from canvas_mcp.tools import peer_review_comments, peer_reviews


def capture(module, registration):
    mcp = FastMCP("private-peer-reports")
    functions = {}

    def tool(*args, **kwargs):
        def register(fn):
            functions[fn.__name__] = fn
            return fn

        return register

    mcp.tool = tool
    getattr(module, registration)(mcp)
    return functions


@pytest.fixture(params=["report", "dataset"])
def local_tool(request):
    dataset = request.param == "dataset"
    module = peer_review_comments if dataset else peer_reviews
    registration = (
        "register_peer_review_comment_tools"
        if dataset
        else "register_peer_review_tools"
    )
    name = "extract_peer_review_dataset" if dataset else "generate_peer_review_report"
    analyzer = MagicMock()
    analyzer.generate_report = AsyncMock(
        return_value={"report": "Sensitive Student Name"}
    )
    analyzer.get_peer_review_comments = AsyncMock(
        return_value={
            "assignment_info": {"assignment_name": "Sensitive Student Name"},
            "peer_reviews": [
                {
                    "reviewer": {"student_name": "=FORMULA()"},
                    "reviewee": {"student_name": "Sensitive Student Name"},
                    "review_content": {"comment_text": "=SUM(1,2)"},
                }
            ],
        }
    )
    analyzer.analyze_peer_review_quality = AsyncMock(return_value={})
    with (
        patch.object(module, "get_course_id", AsyncMock(return_value="12")),
        patch.object(
            module,
            "PeerReviewCommentAnalyzer" if dataset else "PeerReviewAnalyzer",
            return_value=analyzer,
        ),
        patch.object(module, "is_http_request_active", return_value=False) as http,
    ):
        yield module, capture(module, registration)[name], analyzer, http, dataset


async def invoke(tool, dataset, filename=None, format=None, local=True):
    kwargs = {"filename": filename}
    if dataset:
        kwargs.update(
            save_locally=local, include_analytics=False, output_format=format or "csv"
        )
    else:
        kwargs.update(save_to_file=local, report_format=format or "markdown")
    return await tool(12, 34, **kwargs)


async def test_private_bundle_permissions_and_no_overwrite(local_tool, tmp_path):
    module, tool, analyzer, http, dataset = local_tool
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    flat = root / "existing.csv"
    flat.write_text("keep")
    first = json.loads(await invoke(tool, dataset, str(flat)))
    second = json.loads(await invoke(tool, dataset, str(flat)))
    saved = Path(first["saved_to"])
    assert saved.parent != Path(second["saved_to"]).parent
    assert saved.name == ("dataset.csv" if dataset else "report.md")
    assert flat.read_text() == "keep"
    assert "Sensitive Student Name" not in json.dumps(first)
    assert stat.S_IMODE(saved.stat().st_mode) == 0o600
    assert stat.S_IMODE(saved.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE((saved.parent / "manifest.json").stat().st_mode) == 0o600
    assert (
        json.loads((saved.parent / "manifest.json").read_text())["state"] == "complete"
    )
    if dataset:
        assert "'=FORMULA()" in saved.read_text()
        assert "'=SUM(1,2)" in saved.read_text()


async def test_existing_public_directory_refused(local_tool, tmp_path):
    module, tool, analyzer, http, dataset = local_tool
    root = tmp_path / "public"
    root.mkdir(mode=0o755)
    result = await invoke(tool, dataset, str(root / "report.csv"))
    assert result.startswith("Error:")
    assert not list(root.iterdir())
    assert "Sensitive Student Name" not in result


async def test_symlink_directory_cannot_escape(local_tool, tmp_path):
    module, tool, analyzer, http, dataset = local_tool
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    link = tmp_path / "link"
    link.symlink_to(outside, target_is_directory=True)
    result = await invoke(tool, dataset, str(link / "report.csv"))
    assert result.startswith("Error:")
    assert not list(outside.iterdir())


@pytest.mark.parametrize(
    "filename",
    ["../report.csv", "unsafe.xlsx", "Sensitive Student Name.csv", ".csv", "a.csv.exe"],
)
async def test_invalid_filename_refused_without_artifact(
    local_tool, tmp_path, monkeypatch, filename
):
    module, tool, analyzer, http, dataset = local_tool
    monkeypatch.chdir(tmp_path)
    result = await invoke(tool, dataset, filename)
    assert result.startswith("Error:")
    assert not list(tmp_path.iterdir())
    assert "Sensitive Student Name" not in result


async def test_http_refused_before_sensitive_fetch(local_tool):
    module, tool, analyzer, http, dataset = local_tool
    http.return_value = True
    result = await invoke(tool, dataset)
    assert "local (stdio)" in result
    analyzer.generate_report.assert_not_awaited()
    analyzer.get_peer_review_comments.assert_not_awaited()


async def test_save_failure_does_not_disclose_raw_exception(local_tool, tmp_path):
    module, tool, analyzer, http, dataset = local_tool
    with patch.object(
        module,
        "write_private_bundle",
        side_effect=OSError("Sensitive Student Name /secret/path"),
    ):
        result = await invoke(tool, dataset, str(tmp_path / "report.csv"))
    assert result.startswith("Error:")
    assert "Sensitive Student Name" not in result
    assert "/secret/path" not in result


async def test_analyzer_failure_local_redacted_inline_unchanged(local_tool):
    module, tool, analyzer, http, dataset = local_tool
    method = analyzer.get_peer_review_comments if dataset else analyzer.generate_report
    method.return_value = {"error": "Sensitive Student Name"}
    local = await invoke(tool, dataset)
    inline = await invoke(tool, dataset, local=False)
    assert "Sensitive Student Name" not in local
    assert "Sensitive Student Name" in inline


async def test_json_exports_are_saved(local_tool, tmp_path):
    module, tool, analyzer, http, dataset = local_tool
    analyzer.generate_report.return_value = {
        "analytics": {"student_name": "Sensitive Student Name"}
    }
    result = json.loads(
        await invoke(tool, dataset, str(tmp_path / "report.json"), format="json")
    )
    data = json.loads(Path(result["saved_to"]).read_text())
    assert "Sensitive Student Name" in json.dumps(data)
    assert "Sensitive Student Name" not in json.dumps(result)


async def test_default_root_created_private(local_tool, tmp_path, monkeypatch):
    module, tool, analyzer, http, dataset = local_tool
    monkeypatch.chdir(tmp_path)
    result = json.loads(await invoke(tool, dataset))
    root = tmp_path / ("exports" if dataset else "reports")
    assert Path(result["saved_to"]).parent.parent == root
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
