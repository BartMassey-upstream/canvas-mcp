"""Private, exclusively created Linux artifact bundles."""

import hashlib
import json
import os
import re
import stat
import uuid
from pathlib import Path
from typing import Any


def _private_directory(path: Path, *, create: bool = True) -> int:
    """Open a destination without following any symlink components."""
    if os.name != "posix" or not hasattr(os, "O_NOFOLLOW"):
        raise ValueError("Private artifact storage requires POSIX directory handles")
    if ".." in path.parts:
        raise ValueError("Parent traversal is not permitted")
    absolute = path.absolute()
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(absolute.anchor, flags)
    try:
        for component in absolute.parts[1:]:
            if create:
                try:
                    os.mkdir(component, mode=0o700, dir_fd=fd)
                except FileExistsError:
                    pass
                else:
                    os.fsync(fd)
            child = os.open(component, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        info = os.fstat(fd)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise ValueError("Destination must be owned by the current user with mode 0700")
        return fd
    except BaseException:
        os.close(fd)
        raise


def write_private_bundle(
    destination: str,
    namespace: str,
    files: dict[str, bytes],
    metadata: dict[str, Any],
) -> Path:
    """Publish a new immutable bundle; manifest.json marks completion.

    Consumers must validate manifest metadata and file digests before use.
    Failed writes are removed when possible; interrupted bundles without a
    valid manifest are incomplete and must never be treated as usable data.
    """
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", namespace):
        raise ValueError("Invalid artifact namespace")
    if not files or any(
        not re.fullmatch(r"[a-zA-Z0-9_-]+\.[a-z0-9]+", name)
        or name in {"manifest.json", "manifest.tmp"}
        for name in files
    ):
        raise ValueError("Invalid artifact filenames")
    path = Path(destination).expanduser()
    root_fd = _private_directory(path)
    bundle_fd = None
    created: list[str] = []
    bundle = f"{namespace}-{uuid.uuid4().hex}"
    made_bundle = False
    try:
        os.mkdir(bundle, mode=0o700, dir_fd=root_fd)
        made_bundle = True
        bundle_fd = os.open(
            bundle, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd
        )
        os.fchmod(bundle_fd, 0o700)
        manifest = dict(metadata)
        manifest["files"] = {
            name: {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
            for name, data in files.items()
        }
        manifest["state"] = "complete"
        payloads = dict(files)
        payloads["manifest.tmp"] = json.dumps(manifest, sort_keys=True).encode()
        for name, data in payloads.items():
            fd = os.open(
                name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600, dir_fd=bundle_fd,
            )
            created.append(name)
            with os.fdopen(fd, "wb") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        os.link(
            "manifest.tmp", "manifest.json",
            src_dir_fd=bundle_fd, dst_dir_fd=bundle_fd, follow_symlinks=False,
        )
        created.append("manifest.json")
        os.unlink("manifest.tmp", dir_fd=bundle_fd)
        created.remove("manifest.tmp")
        os.fsync(bundle_fd)
        os.fsync(root_fd)
        return path.absolute() / bundle
    except BaseException:
        if bundle_fd is not None:
            for name in reversed(created):
                try:
                    os.unlink(name, dir_fd=bundle_fd)
                except OSError:
                    pass
        if made_bundle:
            try:
                os.rmdir(bundle, dir_fd=root_fd)
            except OSError:
                pass
        raise
    finally:
        if bundle_fd is not None:
            os.close(bundle_fd)
        os.close(root_fd)
