"""Exact fixture exceptions must never hide new or verified credentials."""

import importlib.util
import json
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/check_trufflehog.py"
SPEC = importlib.util.spec_from_file_location("check_trufflehog", SCRIPT)
assert SPEC and SPEC.loader
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)


@pytest.fixture
def finding():
    return {
        "DetectorName": "URI",
        "Verified": False,
        "Raw": "dummy credential",
        "RawV2": "dummy credential with context",
        "SourceMetadata": {"Data": {"Git": {"file": "tests/fixture.py"}}},
    }


def report(tmp_path, *findings):
    path = tmp_path / "scan.jsonl"
    path.write_text("".join(json.dumps(finding) + "\n" for finding in findings))
    return path


def allow(finding):
    return {(finding["DetectorName"], "tests/fixture.py", gate.fingerprint(finding))}


def test_known_fixture_and_historical_copies_pass(tmp_path, finding):
    historical = deepcopy(finding)
    historical["SourceMetadata"]["Data"]["Git"]["commit"] = "old-commit"
    assert gate.check_report(report(tmp_path, finding, historical), allow(finding), 183) == (2, 0)


@pytest.mark.parametrize("change", ["verified", "raw", "raw_v2", "path", "detector"])
def test_changed_or_verified_credentials_fail(tmp_path, finding, change):
    changed = deepcopy(finding)
    if change == "verified":
        changed["Verified"] = True
    elif change == "raw":
        changed["Raw"] = "different credential"
    elif change == "raw_v2":
        changed["RawV2"] = "different credential context"
    elif change == "path":
        changed["SourceMetadata"]["Data"]["Git"]["file"] = "src/production.py"
    else:
        changed["DetectorName"] = "OtherDetector"
    assert gate.check_report(report(tmp_path, changed), allow(finding), 183) == (0, 1)


def test_verification_errors_do_not_allow_unknown_findings(tmp_path, finding):
    finding["VerificationError"] = "network unavailable"
    assert gate.check_report(report(tmp_path, finding), set(), 183) == (0, 1)


@pytest.mark.parametrize("exit_code", [1, 2, -9])
def test_scanner_errors_fail_even_with_allowlisted_results(tmp_path, finding, exit_code):
    with pytest.raises(ValueError):
        gate.check_report(report(tmp_path, finding), allow(finding), exit_code)


@pytest.mark.parametrize("with_finding,exit_code", [(False, 183), (True, 0)])
def test_exit_code_must_agree_with_report(tmp_path, finding, with_finding, exit_code):
    findings = [finding] if with_finding else []
    with pytest.raises(ValueError):
        gate.check_report(report(tmp_path, *findings), allow(finding), exit_code)


def test_empty_successful_scan_passes(tmp_path):
    assert gate.check_report(report(tmp_path), set(), 0) == (0, 0)


def test_filesystem_paths_are_relative_to_scan_root(tmp_path, finding):
    allowed = allow(finding)
    finding["SourceMetadata"]["Data"] = {
        "Filesystem": {"file": str(tmp_path / "tests/fixture.py")}
    }
    assert gate.check_report(report(tmp_path, finding), allowed, 183, tmp_path) == (1, 0)
    with pytest.raises(ValueError):
        gate.check_report(report(tmp_path, finding), allowed, 183, tmp_path / "elsewhere")


@pytest.mark.parametrize("text", ['not-json', '{}', '{"Verified": "false"}'])
def test_malformed_reports_fail_without_echoing_contents(tmp_path, text):
    path = tmp_path / "bad.jsonl"
    path.write_text(text)
    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(path), "--scan-exit-code", "183"],
        text=True, capture_output=True,
    )
    assert result.returncode == 2
    assert result.stderr == "Secret scan failed or its report/allowlist is invalid.\n"


def test_cli_blocks_without_printing_credentials(tmp_path, finding):
    path = report(tmp_path, finding)
    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(path), "--scan-exit-code", "183"],
        text=True, capture_output=True,
    )
    assert result.returncode == 1
    assert "1 blocking findings" in result.stdout
    assert finding["Raw"] not in result.stdout + result.stderr


def test_checked_in_allowlist_has_documented_exact_exceptions():
    entries = gate.load_allowlist(gate.DEFAULT_ALLOWLIST)
    assert len(entries) == 7
    assert all(detector == "URI" for detector, _, _ in entries)


@pytest.mark.parametrize("content", [
    {"version": 2, "entries": []},
    {"version": 1, "entries": [{}]},
    {"version": 1, "entries": [{"path": "../outside"}]},
])
def test_malformed_allowlist_fails(tmp_path, content):
    path = tmp_path / "allowlist.json"
    path.write_text(json.dumps(content))
    with pytest.raises((ValueError, KeyError)):
        gate.load_allowlist(path)
