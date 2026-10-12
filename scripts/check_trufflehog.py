#!/usr/bin/env python3
"""Gate TruffleHog JSON results with exact, reviewed fixture exceptions.

An exception matches the detector, repository-relative path, and SHA-256
of both Raw fields. Verified credentials are never exempt. This also
handles historical fixture copies without excluding their entire files.
The report can contain secrets: print only counts, never raw findings.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ALLOWLIST = ROOT / ".github/trufflehog-allowlist.json"


def fingerprint(finding: dict) -> str:
    """Hash both scanner representations so changed fixture URLs fail."""
    values = [finding["Raw"], finding["RawV2"]]
    if not all(isinstance(value, str) and value for value in values):
        raise ValueError("Invalid credential fields")
    return hashlib.sha256(json.dumps(values, separators=(",", ":")).encode()).hexdigest()


def load_allowlist(path: Path) -> set[tuple[str, str, str]]:
    """Load exact exceptions; malformed configuration must fail closed."""
    data = json.loads(path.read_text())
    if data.get("version") != 1 or not isinstance(data.get("entries"), list):
        raise ValueError("Invalid allowlist schema")
    allowed = set()
    for entry in data["entries"]:
        path_text = entry["path"]
        if (
            not isinstance(path_text, str)
            or not path_text
            or PurePosixPath(path_text).is_absolute()
            or ".." in PurePosixPath(path_text).parts
            or not isinstance(entry["detector"], str)
            or not entry["detector"]
            or not isinstance(entry["reason"], str)
            or not entry["reason"].strip()
            or not re.fullmatch(r"[0-9a-f]{64}", entry["fingerprint"])
        ):
            raise ValueError("Invalid allowlist entry")
        allowed.add((entry["detector"], path_text, entry["fingerprint"]))
    return allowed


def check_report(
    report: Path,
    allowed: set[tuple[str, str, str]],
    scan_exit_code: int,
    filesystem_root: Path | None = None,
) -> tuple[int, int]:
    """Return reviewed and blocking counts, rejecting failed/incomplete scans."""
    if scan_exit_code not in (0, 183):
        raise ValueError("TruffleHog scan failed; inspect the scanner error log")
    reviewed = blocked = 0
    for line in report.read_text().splitlines():
        if not line.strip():
            continue
        finding = json.loads(line)
        if type(finding["Verified"]) is not bool:
            raise ValueError("Invalid verification status")
        metadata = finding["SourceMetadata"]["Data"]
        if filesystem_root is None:
            path = metadata["Git"]["file"]
        else:
            path = (
                Path(metadata["Filesystem"]["file"])
                .resolve()
                .relative_to(filesystem_root.resolve())
                .as_posix()
            )
        key = (finding["DetectorName"], path, fingerprint(finding))
        if not finding["Verified"] and key in allowed:
            reviewed += 1
        else:
            blocked += 1
    if (scan_exit_code == 183) != bool(reviewed + blocked):
        raise ValueError("Scanner exit status disagrees with its report")
    return reviewed, blocked


def main() -> int:
    """Check a private scanner report without publishing its credentials."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--scan-exit-code", type=int, required=True)
    parser.add_argument("--allowlist", type=Path, default=DEFAULT_ALLOWLIST)
    parser.add_argument("--filesystem-root", type=Path)
    args = parser.parse_args()
    try:
        reviewed, blocked = check_report(
            args.report,
            load_allowlist(args.allowlist),
            args.scan_exit_code,
            args.filesystem_root,
        )
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        print("Secret scan failed or its report/allowlist is invalid.", file=sys.stderr)
        return 2
    print(f"Secret scan: {reviewed} reviewed fixture findings; {blocked} blocking findings.")
    return 1 if blocked else 0


if __name__ == "__main__":
    raise SystemExit(main())
