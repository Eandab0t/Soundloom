"""Version parsing, comparison and compatibility constraints.

Versions are strict ``X.Y.Z`` (three dot-separated integers), matching the
component manifest format. Constraints look like ``>=0.2.2`` and use the
same operator set as the manifest validator.
"""
from __future__ import annotations

_OPERATORS = (">=", "<=", "==", "!=", ">", "<")


class VersionError(ValueError):
    """Raised for malformed versions or constraints."""


def parse_version(text: str) -> tuple[int, int, int]:
    parts = text.split(".")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        raise VersionError(f"version {text!r} is not X.Y.Z")
    return (int(parts[0]), int(parts[1]), int(parts[2]))


def compare_versions(a: str, b: str) -> int:
    """Return negative/zero/positive as a<b, a==b, a>b."""
    pa, pb = parse_version(a), parse_version(b)
    return (pa > pb) - (pa < pb)


def satisfies(version: str, constraint: str) -> bool:
    """True when ``version`` fulfils ``constraint`` (e.g. '>=0.2.2')."""
    for operator in _OPERATORS:
        if constraint.startswith(operator):
            target = constraint[len(operator):]
            break
    else:
        raise VersionError(f"constraint {constraint!r} has no operator")
    result = compare_versions(version, target)
    return {
        ">=": result >= 0,
        "<=": result <= 0,
        "==": result == 0,
        "!=": result != 0,
        ">": result > 0,
        "<": result < 0,
    }[operator]


def is_newer(candidate: str, installed: str) -> bool:
    """True when candidate is strictly newer than installed.

    Downgrade protection leans on this: anything not strictly newer is
    never offered as an update.
    """
    return compare_versions(candidate, installed) > 0
