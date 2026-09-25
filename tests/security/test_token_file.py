"""Tests for the per-user Canvas token file."""

import importlib
import os

import pytest

from canvas_mcp.core import config as config_module


def _write_token_file(path, content: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(mode)
    if os.name == "nt":
        ntsecuritycon = importlib.import_module("ntsecuritycon")
        win32api = importlib.import_module("win32api")
        win32con = importlib.import_module("win32con")
        win32security = importlib.import_module("win32security")
        process_token = win32security.OpenProcessToken(
            win32api.GetCurrentProcess(), win32con.TOKEN_QUERY
        )
        try:
            current_sid = win32security.GetTokenInformation(
                process_token, win32security.TokenUser
            )[0]
        finally:
            process_token.Close()
        acl = win32security.ACL()
        access = ntsecuritycon.FILE_GENERIC_READ | ntsecuritycon.FILE_GENERIC_WRITE
        for sid in (
            current_sid,
            win32security.CreateWellKnownSid(win32security.WinLocalSystemSid, None),
            win32security.CreateWellKnownSid(
                win32security.WinBuiltinAdministratorsSid, None
            ),
        ):
            acl.AddAccessAllowedAce(win32security.ACL_REVISION, access, sid)
        descriptor = win32security.GetFileSecurity(
            str(path),
            win32security.OWNER_SECURITY_INFORMATION
            | win32security.DACL_SECURITY_INFORMATION,
        )
        descriptor.SetSecurityDescriptorDacl(True, acl, False)
        win32security.SetFileSecurity(
            str(path), win32security.DACL_SECURITY_INFORMATION, descriptor
        )


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


@pytest.mark.skipif(os.name == "nt", reason="Windows uses ACLs instead of mode bits")
@pytest.mark.parametrize("mode", [0o400, 0o640, 0o644])
def test_token_file_requires_exactly_0600(monkeypatch, tmp_path, mode):
    token_path = tmp_path / ".canvas-mcp"
    _write_token_file(token_path, "file-token\n", mode)
    monkeypatch.setattr(config_module, "_canvas_token_file_path", lambda: token_path)
    monkeypatch.setenv("CANVAS_API_TOKEN", "must-not-be-used")

    with pytest.raises(config_module.CanvasTokenFileError, match="permissions 0600"):
        config_module.get_config()


def test_legacy_home_token_file_remains_a_fallback(monkeypatch, tmp_path):
    native_path = tmp_path / "canvas-mcp/token"
    legacy_path = tmp_path / ".canvas-mcp"
    _write_token_file(legacy_path, "legacy-token\n")
    monkeypatch.setattr(config_module, "_canvas_token_file_path", lambda: native_path)
    monkeypatch.setattr(
        config_module, "_legacy_canvas_token_file_path", lambda: legacy_path
    )

    assert config_module.get_config().canvas_api_token == "legacy-token"


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL test")
def test_windows_token_acl_rejects_other_principals(monkeypatch, tmp_path):
    token_path = tmp_path / "canvas-mcp/token"
    _write_token_file(token_path, "file-token")
    ntsecuritycon = importlib.import_module("ntsecuritycon")
    win32security = importlib.import_module("win32security")
    descriptor = win32security.GetFileSecurity(
        str(token_path), win32security.DACL_SECURITY_INFORMATION
    )
    acl = descriptor.GetSecurityDescriptorDacl()
    everyone_sid = win32security.CreateWellKnownSid(
        win32security.WinWorldSid, None
    )
    acl.AddAccessAllowedAce(
        win32security.ACL_REVISION, ntsecuritycon.FILE_GENERIC_READ, everyone_sid
    )
    descriptor.SetSecurityDescriptorDacl(True, acl, False)
    win32security.SetFileSecurity(
        str(token_path), win32security.DACL_SECURITY_INFORMATION, descriptor
    )
    monkeypatch.setattr(config_module, "_canvas_token_file_path", lambda: token_path)

    with pytest.raises(config_module.CanvasTokenFileError, match="unauthorized principal"):
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
