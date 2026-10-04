"""Inspect built archives without extracting private data or trusting paths."""

import argparse
import hashlib
import json
import re
import stat
import tarfile
import zipfile
from pathlib import Path, PurePosixPath

PRIVATE_DIRECTORIES = {
    "local_maps",
    "local_snapshots",
    "reports",
    "snapshots",
    "student_records",
    "exports",
    ".venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".git",
}
PRIVATE_FILES = {
    "token",
    ".env",
    "identities.json",
    "anonymization_map.csv",
    "records.jsonl",
    "checkpoint.json",
}


def check_member(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if not name or "\\" in name or path.is_absolute() or ".." in path.parts:
        raise ValueError("Unsafe archive member path")
    if any(
        part in PRIVATE_DIRECTORIES
        or (part.startswith("_") and not part.startswith("__"))
        for part in path.parts
    ):
        raise ValueError("Private or scratch directory in distribution")
    if (
        path.name in PRIVATE_FILES
        or path.suffix in {".imscc", ".pyc"}
        or (path.name.startswith("anonymization_map_") and path.suffix == ".csv")
        or re.fullmatch(
            r"records-[a-f0-9]{32}\.jsonl|checkpoint-[0-9]{6}\.json", path.name
        )
    ):
        raise ValueError("Private artifact in distribution")
    return path


def inspect_distribution(archive: Path, expected_package_files: set[str]) -> dict:
    members = []
    if archive.suffix == ".whl":
        kind = "wheel"
        with zipfile.ZipFile(archive) as package:
            for member in package.infolist():
                path = check_member(member.filename)
                if stat.S_ISLNK(member.external_attr >> 16):
                    raise ValueError("Archive links are not permitted")
                if not member.is_dir():
                    members.append(str(path))
            entries = [
                name for name in members if name.endswith(".dist-info/entry_points.txt")
            ]
            if (
                len(entries) != 1
                or "canvas-mcp-server = canvas_mcp.server:main"
                not in package.read(entries[0]).decode()
            ):
                raise ValueError("Missing Canvas server entry point")
        expected = expected_package_files
    elif archive.name.endswith(".tar.gz"):
        kind = "sdist"
        with tarfile.open(archive, "r:gz") as package:
            for member in package.getmembers():
                path = check_member(member.name)
                if not member.isfile() and not member.isdir():
                    raise ValueError(
                        "Archive links and special files are not permitted"
                    )
                if member.isfile():
                    members.append(str(path))
        roots = {PurePosixPath(name).parts[0] for name in members}
        if len(roots) != 1:
            raise ValueError("Expected one source distribution root")
        root = next(iter(roots))
        members = [str(PurePosixPath(name).relative_to(root)) for name in members]
        expected = {f"src/{name}" for name in expected_package_files} | {
            "pyproject.toml",
            "uv.lock",
            "README.md",
            "LICENSE",
        }
    else:
        raise ValueError("Expected wheel or .tar.gz source distribution")
    if len(members) != len(set(members)):
        raise ValueError("Duplicate archive members")
    missing = sorted(expected - set(members))
    if missing:
        raise ValueError(f"Missing expected package assets: {missing}")
    return {
        "file": archive.name,
        "kind": kind,
        "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
        "entries": len(members),
        "private_paths": False,
        "required_assets": len(expected),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist", type=Path, required=True)
    parser.add_argument(
        "--source", type=Path, default=Path(__file__).resolve().parents[1] / "src"
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    expected = {
        str(path.relative_to(args.source))
        for path in (args.source / "canvas_mcp").rglob("*")
        if path.is_file() and path.suffix in {".py", ".ts", ".md"}
    }
    if not {"canvas_mcp/server.py", "canvas_mcp/code_api/index.ts"} <= expected:
        raise ValueError("Source package inventory is incomplete")
    wheels = list(args.dist.glob("*.whl"))
    sources = list(args.dist.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sources) != 1:
        raise ValueError("Expected exactly one wheel and one sdist")
    results = [inspect_distribution(path, expected) for path in [sources[0], wheels[0]]]
    args.output.write_text(json.dumps(results, indent=2) + "\n")


if __name__ == "__main__":
    main()
