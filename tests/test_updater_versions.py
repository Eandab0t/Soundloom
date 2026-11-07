"""Tests for backend/updater/versions: parsing, comparison, constraints."""
import pytest

from backend.updater import versions
from backend.updater.versions import VersionError


class TestParseVersion:
    @pytest.mark.parametrize("text,expected", [
        ("0.2.2", (0, 2, 2)),
        ("1.0.0", (1, 0, 0)),
        ("10.20.30", (10, 20, 30)),
    ])
    def test_parses(self, text, expected):
        assert versions.parse_version(text) == expected

    @pytest.mark.parametrize("text", ["1.0", "1.0.0.0", "v1.0.0", "1.x.0", "", "1.0.0-rc1"])
    def test_rejects_malformed(self, text):
        with pytest.raises(VersionError):
            versions.parse_version(text)


class TestCompareVersions:
    @pytest.mark.parametrize("a,b,sign", [
        ("1.0.0", "1.0.0", 0),
        ("1.0.1", "1.0.0", 1),
        ("1.1.0", "1.0.9", 1),
        ("2.0.0", "1.9.9", 1),
        ("0.2.2", "0.2.10", -1),   # numeric, not lexicographic
    ])
    def test_compares(self, a, b, sign):
        assert (versions.compare_versions(a, b) > 0) - (versions.compare_versions(a, b) < 0) == sign


class TestSatisfies:
    @pytest.mark.parametrize("version,constraint,expected", [
        ("0.2.2", ">=0.2.2", True),
        ("0.2.1", ">=0.2.2", False),
        ("0.3.0", ">=0.2.2", True),
        ("1.0.0", "==1.0.0", True),
        ("1.0.1", "==1.0.0", False),
        ("0.2.2", "<0.3.0", True),
        ("0.3.0", "<0.3.0", False),
        ("0.2.9", ">0.2.2", True),
        ("0.2.2", ">0.2.2", False),
        ("0.3.0", "!=0.2.2", True),
        ("0.2.2", "!=0.2.2", False),
        ("0.2.2", "<=0.2.2", True),
    ])
    def test_constraints(self, version, constraint, expected):
        assert versions.satisfies(version, constraint) is expected

    @pytest.mark.parametrize("bad", ["0.2.2", "~0.2.2", ">=1.0"])
    def test_malformed_constraint_raises(self, bad):
        with pytest.raises(VersionError):
            versions.satisfies("0.2.2", bad)


class TestIsNewer:
    def test_strictly_newer_only(self):
        assert versions.is_newer("1.0.1", "1.0.0") is True
        assert versions.is_newer("1.0.0", "1.0.0") is False
        assert versions.is_newer("1.0.0", "1.0.1") is False  # no downgrades
