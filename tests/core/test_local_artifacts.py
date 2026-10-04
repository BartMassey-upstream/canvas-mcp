"""Private artifact persistence under permissive umasks and failures."""

import hashlib
import json
import os
import stat
from unittest.mock import patch

import pytest

from canvas_mcp.core.local_artifacts import write_private_bundle


def test_bundle_permissions_integrity_and_no_overwrite(tmp_path):
    root = tmp_path / "private"
    old_mask = os.umask(0)
    try:
        first = write_private_bundle(str(root), "test-v1", {"data.json": b"original"}, {})
        second = write_private_bundle(str(root), "test-v1", {"data.json": b"changed"}, {})
    finally:
        os.umask(old_mask)
    assert first != second
    assert (first / "data.json").read_bytes() == b"original"
    for directory in (root, first, second):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
        for child in directory.iterdir():
            if child.is_file():
                assert stat.S_IMODE(child.stat().st_mode) == 0o600
    manifest = json.loads((first / "manifest.json").read_text())
    assert manifest["state"] == "complete"
    assert manifest["files"]["data.json"] == {
        "bytes": 8, "sha256": hashlib.sha256(b"original").hexdigest()
    }
    assert not (first / "manifest.tmp").exists()


@pytest.mark.parametrize("ancestor", [True, False])
def test_symlink_components_refused(tmp_path, ancestor):
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(OSError):
        write_private_bundle(str(link / "child" if ancestor else link), "test", {"x.json": b"x"}, {})
    assert list(target.iterdir()) == []


def test_insecure_existing_destination_and_traversal_refused(tmp_path):
    root = tmp_path / "public"
    root.mkdir(mode=0o755)
    with pytest.raises(ValueError):
        write_private_bundle(str(root), "test", {"x.json": b"x"}, {})
    with pytest.raises(ValueError):
        write_private_bundle(str(tmp_path / ".." / "escape"), "test", {"x.json": b"x"}, {})
    for name in ("../escape", "manifest.json", "manifest.tmp", "a/b.json"):
        with pytest.raises(ValueError):
            write_private_bundle(str(tmp_path / "safe"), "test", {name: b"x"}, {})


@pytest.mark.parametrize("failure", [OSError("disk full"), KeyboardInterrupt()])
def test_interrupted_write_removes_owned_partial_bundle(tmp_path, failure):
    root = tmp_path / "private"
    with patch("canvas_mcp.core.local_artifacts.os.fsync", side_effect=failure):
        with pytest.raises(type(failure)):
            write_private_bundle(str(root), "test", {"x.json": b"secret"}, {})
    assert list(root.iterdir()) == []


def test_bundle_collision_does_not_remove_previous_artifact(tmp_path):
    root = tmp_path / "private"
    with patch("canvas_mcp.core.local_artifacts.uuid.uuid4") as uuid:
        uuid.return_value.hex = "fixed"
        first = write_private_bundle(str(root), "test", {"x.json": b"first"}, {})
        with pytest.raises(FileExistsError):
            write_private_bundle(str(root), "test", {"x.json": b"second"}, {})
    assert (first / "x.json").read_bytes() == b"first"
    assert (first / "manifest.json").exists()


def test_failed_cleanup_leaves_no_completion_marker(tmp_path):
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    real_unlink = os.unlink

    def fail_data_cleanup(name, **kwargs):
        if name == "x.json":
            raise OSError("cleanup refused")
        return real_unlink(name, **kwargs)

    with patch("canvas_mcp.core.local_artifacts.os.fsync", side_effect=OSError), patch(
        "canvas_mcp.core.local_artifacts.os.unlink", side_effect=fail_data_cleanup
    ):
        with pytest.raises(OSError):
            write_private_bundle(str(root), "test", {"x.json": b"secret"}, {})
    bundle = next(root.iterdir())
    assert not (bundle / "manifest.json").exists()
    assert stat.S_IMODE((bundle / "x.json").stat().st_mode) == 0o600


def test_new_destination_ancestors_are_synced(tmp_path):
    real_fsync = os.fsync
    synced = []

    def track_fsync(fd):
        info = os.fstat(fd)
        if stat.S_ISDIR(info.st_mode):
            synced.append((info.st_dev, info.st_ino))
        real_fsync(fd)

    root = tmp_path / 'new-parent' / 'private'
    with patch('canvas_mcp.core.local_artifacts.os.fsync', side_effect=track_fsync):
        bundle = write_private_bundle(str(root), 'test', {'x.json': b'x'}, {})
    expected = []
    for directory in (tmp_path, root.parent, bundle, root):
        info = directory.stat()
        expected.append((info.st_dev, info.st_ino))
    assert synced == expected
