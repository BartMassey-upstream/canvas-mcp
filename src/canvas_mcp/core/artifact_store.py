"""Bounded private artifact access and atomic, exclusive checkpoints."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import stat
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from .local_artifacts import _private_directory

MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
MAX_RECORD_BYTES = 1024 * 1024


def canonical_origin(url: str) -> str:
    from urllib.parse import urlsplit

    parsed = urlsplit(url)
    if (
        parsed.scheme not in {"https", "http"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise ValueError("Invalid Canvas origin")
    host = parsed.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    port = parsed.port
    suffix = (
        f":{port}" if port and port != {"https": 443, "http": 80}[parsed.scheme] else ""
    )
    return f"{parsed.scheme}://{host}{suffix}"


def json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode()


class ArtifactStore:
    """Access a private directory through retained, non-symlink handles."""

    def __init__(self, path: str | Path, *, create: bool = False) -> None:
        self.path = Path(path).expanduser().absolute()
        self.fd = _private_directory(self.path, create=create)

    def __enter__(self) -> ArtifactStore:
        return self

    def __exit__(self, *_args: Any) -> None:
        os.close(self.fd)

    def lock(self, *, shared: bool = False) -> None:
        import fcntl

        fcntl.flock(
            self.fd, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB
        )

    @staticmethod
    def _name(name: str) -> str:
        if not isinstance(name, str) or not re.fullmatch(
            r"[a-zA-Z0-9_-]+\.[a-z0-9]+", name
        ):
            raise ValueError("Invalid artifact member name")
        return name

    def open_read(self, name: str, limit: int = MAX_FILE_BYTES) -> int:
        fd = os.open(
            self._name(name),
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=self.fd,
        )
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
            or info.st_size > limit
        ):
            os.close(fd)
            raise ValueError("Artifact member is not a bounded private regular file")
        return fd

    def read(self, name: str, limit: int = MAX_FILE_BYTES) -> bytes:
        with os.fdopen(self.open_read(name, limit), "rb") as stream:
            data = stream.read(limit + 1)
        if len(data) > limit:
            raise ValueError("Artifact size limit exceeded")
        return data

    @staticmethod
    def decode_json(data: bytes) -> Any:
        def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, value in items:
                if key in result:
                    raise ValueError("Duplicate JSON key")
                result[key] = value
            return result

        return json.loads(
            data,
            object_pairs_hook=pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(
                ValueError("Nonfinite JSON")
            ),
        )

    def read_json(self, name: str, limit: int = MAX_MANIFEST_BYTES) -> Any:
        return self.decode_json(self.read(name, limit))

    def write(self, name: str, data: bytes) -> dict[str, Any]:
        self._name(name)
        if len(data) > MAX_FILE_BYTES:
            raise ValueError("Artifact size limit exceeded")
        temporary = f"pending-{uuid.uuid4().hex}.tmp"
        fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=self.fd,
        )
        try:
            with os.fdopen(fd, "wb") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.link(
                temporary,
                name,
                src_dir_fd=self.fd,
                dst_dir_fd=self.fd,
                follow_symlinks=False,
            )
            os.unlink(temporary, dir_fd=self.fd)
            os.fsync(self.fd)
        finally:
            try:
                os.unlink(temporary, dir_fd=self.fd)
            except FileNotFoundError:
                pass
        return {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}

    @staticmethod
    def decode_records(data: bytes) -> Iterator[dict[str, Any]]:
        stream = io.BytesIO(data)
        while line := stream.readline(MAX_RECORD_BYTES + 1):
            if len(line) > MAX_RECORD_BYTES:
                raise ValueError("Oversized archive record")
            record = ArtifactStore.decode_json(line)
            if not isinstance(record, dict):
                raise ValueError("Invalid archive record")
            yield record

    def total_size(self) -> int:
        total = 0
        for name in self.names():
            info = os.stat(name, dir_fd=self.fd, follow_symlinks=False)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_nlink != 1
            ):
                raise ValueError("Nonprivate or nonregular archive member")
            total += info.st_size
            if total > MAX_ARCHIVE_BYTES:
                raise ValueError("Archive size limit exceeded")
        return total

    def records(self, name: str) -> Iterator[dict[str, Any]]:
        with os.fdopen(self.open_read(name), "rb") as stream:
            while line := stream.readline(MAX_RECORD_BYTES + 1):
                if len(line) > MAX_RECORD_BYTES:
                    raise ValueError("Oversized archive record")
                record = self.decode_json(line)
                if not isinstance(record, dict):
                    raise ValueError("Invalid archive record")
                yield record

    def names(self) -> list[str]:
        names = os.listdir(self.fd)
        if len(names) > 20000:
            raise ValueError("Too many artifact files")
        return names
