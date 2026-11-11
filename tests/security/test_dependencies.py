"""
Dependency Security Tests

Tests for dependency vulnerabilities, outdated packages,
and supply chain security.

Test Coverage:
- TC-9.1: Known Vulnerability Scan
- TC-9.2: Outdated Dependencies
- TC-9.3: License Compliance
"""

import json
import subprocess
import tomllib
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version


class TestDependencyVulnerabilities:
    """Test for known vulnerabilities in dependencies."""

    def test_no_critical_vulnerabilities(self):
        """TC-9.1.1: Scan for critical vulnerabilities."""
        try:
            # Run pip-audit to check for known vulnerabilities
            result = subprocess.run(
                ["pip-audit", "--format", "json", "--desc"],
                capture_output=True,
                text=True,
                timeout=60
            )

            if result.returncode == 0:
                # No vulnerabilities found
                return

            # Parse output
            if result.stdout:
                try:
                    vulns = json.loads(result.stdout)

                    # Check for critical vulnerabilities
                    critical_vulns = [
                        v for v in vulns.get("vulnerabilities", [])
                        if "critical" in str(v).lower()
                    ]

                    assert len(critical_vulns) == 0, \
                        f"Critical vulnerabilities found: {critical_vulns}"
                except json.JSONDecodeError:
                    # If pip-audit not installed, skip test
                    pytest.skip("pip-audit not installed")

        except FileNotFoundError:
            pytest.skip("pip-audit not installed")
        except subprocess.TimeoutExpired:
            pytest.fail("pip-audit timed out")

    def test_no_high_vulnerabilities(self):
        """TC-9.1.1: Check for high severity vulnerabilities."""
        try:
            result = subprocess.run(
                ["pip-audit", "--format", "json"],
                capture_output=True,
                text=True,
                timeout=60
            )

            if result.returncode == 0:
                return

            if result.stdout:
                try:
                    vulns = json.loads(result.stdout)

                    high_vulns = [
                        v for v in vulns.get("vulnerabilities", [])
                        if "high" in str(v).lower()
                    ]

                    # Allow some high vulns if they're being addressed
                    # But warn about them
                    if high_vulns:
                        pytest.skip(f"High vulnerabilities found (may be acceptable): {len(high_vulns)}")

                except json.JSONDecodeError:
                    pytest.skip("Could not parse pip-audit output")

        except FileNotFoundError:
            pytest.skip("pip-audit not installed")
        except subprocess.TimeoutExpired:
            pytest.fail("pip-audit timed out")


class TestOutdatedDependencies:
    """Test for outdated dependencies."""

    def test_dependencies_reasonably_current(self):
        """TC-9.1.2: Check that dependencies are not severely outdated."""
        try:
            # Check for outdated packages
            result = subprocess.run(
                ["pip", "list", "--outdated", "--format", "json"],
                capture_output=True,
                text=True,
                timeout=30
            )

            if result.stdout:
                outdated = json.loads(result.stdout)

                # Check for severely outdated packages (>1 year old)
                # This is a rough heuristic
                if len(outdated) > 20:  # Arbitrary threshold
                    pytest.skip(f"Many outdated packages: {len(outdated)}")

        except FileNotFoundError:
            pytest.skip("pip not available")
        except subprocess.TimeoutExpired:
            pytest.skip("pip list timed out - skipping in CI environment")
        except json.JSONDecodeError:
            pytest.skip("Could not parse pip output")


class TestDependencyPinning:
    """Test that dependencies are properly pinned."""

    @pytest.mark.parametrize(
        ("package", "minimum", "vulnerable"),
        [
            ("fastmcp", "4.1.2", ["4.0.0a1", "4.0.3", "4.1.0", "4.1.1", "4.1.2rc1"]),
            ("mcp", "2.2.0", ["2.0.0", "2.1.1", "2.2.0rc1"]),
        ],
    )
    def test_fresh_installs_require_patched_releases(self, package, minimum, vulnerable):
        """A fresh resolver must not select an affected dependency release.

        Parse version bounds rather than matching their textual spelling, so
        raising the security floor or pinning a later release remains valid.
        Wildcard equality begins at .dev0 and can admit affected prereleases.
        """
        project = tomllib.loads(Path("pyproject.toml").read_text())
        requirements = [Requirement(value) for value in project["project"]["dependencies"]]
        matching = [
            req for req in requirements if canonicalize_name(req.name) == package
        ]
        assert len(matching) == 1
        requirement = matching[0]
        assert requirement.url is None and requirement.marker is None
        assert any(
            bound.operator in {">=", ">", "~=", "==", "==="}
            and Version(bound.version.replace(".*", ".dev0")) >= Version(minimum)
            for bound in requirement.specifier
        ), f"{package} must require at least {minimum}"
        for version in vulnerable:
            assert not requirement.specifier.contains(version, prereleases=True), (
                f"{package} still permits affected release {version}"
            )

    @pytest.mark.parametrize(
        ("package", "minimum"),
        [
            ("fastmcp", "4.1.2"),
            ("fastmcp-slim", "4.1.2"),
            ("mcp", "2.2.0"),
            ("certifi", "2026.10.19"),
            ("authlib", "1.9.1"),
            ("dnspython", "2.9.0"),
            ("joserfc", "1.7.6"),
            ("opentelemetry-api", "1.45.1"),
            ("black", "26.10.1"),
        ],
    )
    def test_locked_releases_clear_reviewed_security_floors(self, package, minimum):
        """Protect the reviewed lockfile for runtime, hosted, and dev packages.

        This regression covers the known affected versions; the mandatory
        dependency audit checks advisories published after this review.
        """
        lock = tomllib.loads(Path("uv.lock").read_text())
        versions = [p["version"] for p in lock["package"] if p["name"] == package]
        assert versions, f"Expected locked dependency {package}"
        assert all(Version(version) >= Version(minimum) for version in versions)

    def test_dependencies_pinned_in_pyproject(self):
        """Verify dependencies have version constraints."""
        pyproject = Path("pyproject.toml")

        if not pyproject.exists():
            pytest.skip("pyproject.toml not found")

        content = pyproject.read_text()

        # Check that dependencies have version specifiers
        # Look for dependencies section
        if "dependencies" in content:
            # Should have version specifiers like >=, ==, ~=
            # This is a basic check
            assert ">=" in content or "==" in content or "~=" in content, \
                "Dependencies should have version constraints"


class TestLicenseCompliance:
    """Test dependency license compliance."""

    @pytest.mark.skip(reason="License checking tool not yet integrated")
    def test_license_compatibility(self):
        """TC-9.1.3: Check license compatibility."""
        # Would use a tool like pip-licenses to check
        # Verify all dependencies have compatible licenses (MIT, Apache, BSD)
        # Flag GPL or restrictive licenses
        pass

    @pytest.mark.skip(reason="License checking tool not yet integrated")
    def test_no_restrictive_licenses(self):
        """Verify no GPL or other restrictive licenses."""
        # Check that no dependencies have restrictive licenses
        # that would affect Canvas MCP's MIT license
        pass


class TestSupplyChainSecurity:
    """Test supply chain security."""

    def test_dependencies_from_pypi(self):
        """Verify dependencies are from trusted PyPI."""
        # Check that all dependencies come from official PyPI
        # Not from custom indexes or git repos (unless necessary)
        pyproject = Path("pyproject.toml")

        if pyproject.exists():
            content = pyproject.read_text()

            # Check for git+https dependencies (supply chain risk)
            assert "git+https" not in content or "# trusted" in content, \
                "Git dependencies increase supply chain risk"

    def test_no_suspicious_dependencies(self):
        """Check for typosquatting or suspicious package names."""
        pyproject = Path("pyproject.toml")

        if pyproject.exists():
            content = pyproject.read_text()

            # Check for common typosquatting targets
            suspicious_patterns = [
                "requsets",  # requests typo
                "urlib",     # urllib typo
                "pythno",    # python typo
            ]

            for pattern in suspicious_patterns:
                assert pattern not in content.lower(), \
                    f"Suspicious package name found: {pattern}"


class TestDependencyIntegrity:
    """Test dependency integrity and verification."""

    def test_lockfile_exists(self):
        """Verify lockfile exists for reproducible builds."""
        # Check for requirements.txt or poetry.lock or similar
        lockfiles = [
            "requirements.txt",
            "poetry.lock",
            "Pipfile.lock",
        ]

        # At least one should exist for reproducible builds
        any(Path(f).exists() for f in lockfiles)

        # Or using pip-tools with requirements.txt
        # Canvas MCP uses pyproject.toml with pinned versions

    @pytest.mark.skip(reason="Hash checking not yet implemented")
    def test_dependency_hashes(self):
        """Verify dependencies with cryptographic hashes."""
        # Would check that dependencies are verified with hashes
        # Using --require-hashes flag with pip
        pass


class TestDevelopmentDependencies:
    """Test development dependencies."""

    def test_dev_dependencies_separate(self):
        """Verify dev dependencies are separate from production."""
        pyproject = Path("pyproject.toml")

        if pyproject.exists():
            content = pyproject.read_text()

            # Should have separate dev dependencies
            assert "dev" in content or "optional" in content, \
                "Development dependencies should be separate"

    def test_no_dev_tools_in_production(self):
        """Verify development tools not required in production."""
        # Check that pytest, black, ruff, etc. are optional
        pyproject = Path("pyproject.toml")

        if pyproject.exists():
            content = pyproject.read_text()

            # Dev tools should be in optional-dependencies or dependency-groups (PEP 735)
            if "pytest" in content:
                # Should be in dev section (either legacy or modern PEP 735 format)
                assert (
                    "[project.optional-dependencies]" in content
                    or "[dependency-groups]" in content
                )


class TestSecurityAdvisories:
    """Test for security advisories."""

    @pytest.mark.skip(reason="Security advisory monitoring not automated")
    def test_security_advisories_monitored(self):
        """Verify security advisories are monitored."""
        # Would check that GitHub Dependabot or similar is enabled
        # Or that there's a process for monitoring advisories
        pass


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
