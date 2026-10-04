"""Distribution and interpreter boundaries used by the Linux package gate."""

import importlib.util
import io
import tarfile
import zipfile
from pathlib import Path

import pytest
from run_stdio_acceptance import validate_installed_package

SPEC = importlib.util.spec_from_file_location(
    "verify_distribution",
    Path(__file__).resolve().parents[2] / "scripts/verify_distribution.py",
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
inspect_distribution = MODULE.inspect_distribution


@pytest.mark.parametrize("kind", ["wheel", "sdist"])
@pytest.mark.parametrize(
    "private",
    [
        "_stage3/config/token",
        "local_maps/identities.json",
        "snapshots/records.json",
        "local_snapshots/data.json",
        "records-" + "a" * 32 + ".jsonl",
        "checkpoint-000001.json",
        "anonymization_map_course.csv",
        "nested/../token",
    ],
)
def test_distribution_rejects_private_and_unsafe_members(tmp_path, kind, private):
    archive = make_archive(
        tmp_path, kind, {"canvas_mcp/server.py": b"", private: b"dummy"}
    )
    with pytest.raises(ValueError):
        inspect_distribution(archive, {"canvas_mcp/server.py"})


def make_archive(root, kind, files):
    if kind == "wheel":
        path = root / "canvas.whl"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr(
                "canvas.dist-info/entry_points.txt",
                "[console_scripts]\ncanvas-mcp-server = canvas_mcp.server:main\n",
            )
            for name, data in files.items():
                archive.writestr(name, data)
    else:
        path = root / "canvas.tar.gz"
        with tarfile.open(path, "w:gz") as archive:
            for name, data in files.items():
                member = tarfile.TarInfo(f"canvas/{name}")
                member.size = len(data)
                archive.addfile(member, io.BytesIO(data))
    return path


@pytest.mark.parametrize("kind", ["wheel", "sdist"])
def test_distribution_requires_code_api_assets(tmp_path, kind):
    prefix = "src/" if kind == "sdist" else ""
    files = {
        prefix + "canvas_mcp/server.py": b"",
        **dict.fromkeys(["pyproject.toml", "uv.lock", "README.md", "LICENSE"], b""),
    }
    archive = make_archive(tmp_path, kind, files)
    with pytest.raises(ValueError, match="Missing expected package assets"):
        inspect_distribution(
            archive, {"canvas_mcp/server.py", "canvas_mcp/code_api/index.ts"}
        )


def test_sdist_rejects_link_to_private_content(tmp_path):
    path = tmp_path / "linked.tar.gz"
    with tarfile.open(path, "w:gz") as archive:
        member = tarfile.TarInfo("canvas/innocent.json")
        member.type = tarfile.SYMTYPE
        member.linkname = "../../local_maps/identities.json"
        archive.addfile(member)
    with pytest.raises(ValueError, match="links"):
        inspect_distribution(path, set())


def test_installed_package_boundary_rejects_source_and_wrong_environment(tmp_path):
    environment = tmp_path / "clean"
    python = environment / "bin/python"
    good = {
        "package_path": str(
            environment / "lib/python3.11/site-packages/canvas_mcp/__init__.py"
        ),
        "environment_prefix": str(environment),
    }
    validate_installed_package(good, python)
    for bad in [
        {**good, "package_path": str(tmp_path / "src/canvas_mcp/__init__.py")},
        {**good, "package_path": str(environment / "src/canvas_mcp/__init__.py")},
        {**good, "environment_prefix": str(tmp_path / "different")},
    ]:
        with pytest.raises(AssertionError):
            validate_installed_package(bad, python)


def test_python_symlink_keeps_selected_environment(tmp_path):
    environment = tmp_path / "clean"
    (environment / "bin").mkdir(parents=True)
    python = environment / "bin/python"
    python.symlink_to(Path("/usr/bin/python3"))
    validate_installed_package(
        {
            "package_path": str(
                environment / "lib/site-packages/canvas_mcp/__init__.py"
            ),
            "environment_prefix": str(environment),
        },
        python,
    )
