"""Tests for the per-user Canvas token file."""

import os

import pytest

from canvas_mcp.core import config as config_module


def _write_token_file(path, content: str, mode: int = 0o600) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(mode)


def test_token_file_takes_precedence_over_environment(monkeypatch, tmp_path):
    token_path = tmp_path / ".canvas-mcp"
    _write_token_file(token_path, "file-token\n")
    monkeypatch.setattr(config_module, "_canvas_token_file_path", lambda: token_path)
    monkeypatch.setenv("CANVAS_API_TOKEN", "environment-token")

    assert config_module.get_config().canvas_api_token == "file-token"


def test_environment_is_used_when_token_file_is_absent(monkeypatch, tmp_path):
    token_path = tmp_path / ".canvas-mcp"
    monkeypatch.setattr(config_module, "_canvas_token_file_path", lambda: token_path)
    monkeypatch.setenv("CANVAS_API_TOKEN", "environment-token")

    assert config_module.get_config().canvas_api_token == "environment-token"


@pytest.mark.parametrize("mode", [0o400, 0o640, 0o644])
def test_token_file_requires_exactly_0600(monkeypatch, tmp_path, mode):
    token_path = tmp_path / ".canvas-mcp"
    _write_token_file(token_path, "file-token\n", mode)
    monkeypatch.setattr(config_module, "_canvas_token_file_path", lambda: token_path)
    monkeypatch.setenv("CANVAS_API_TOKEN", "must-not-be-used")

    with pytest.raises(config_module.CanvasTokenFileError, match="permissions 0600"):
        config_module.get_config()


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("\n", "is empty"),
        ("CANVAS_API_TOKEN=not-the-file-format\nextra", "only the token"),
    ],
)
def test_token_file_must_contain_one_nonempty_token(
    monkeypatch, tmp_path, content, message
):
    token_path = tmp_path / ".canvas-mcp"
    _write_token_file(token_path, content)
    monkeypatch.setattr(config_module, "_canvas_token_file_path", lambda: token_path)

    with pytest.raises(config_module.CanvasTokenFileError, match=message):
        config_module.get_config()


@pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="O_NOFOLLOW unavailable")
def test_token_file_symlink_is_rejected(monkeypatch, tmp_path):
    target_path = tmp_path / "real-token"
    _write_token_file(target_path, "file-token")
    token_path = tmp_path / ".canvas-mcp"
    token_path.symlink_to(target_path)
    monkeypatch.setattr(config_module, "_canvas_token_file_path", lambda: token_path)

    with pytest.raises(config_module.CanvasTokenFileError, match="Cannot safely open"):
        config_module.get_config()
