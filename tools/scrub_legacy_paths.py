"""Scrub personal paths out of a file across a git history.

A tree-filter helper for `git filter-branch`: rewrites HANDOFF.md in every
commit so machine-specific paths never reach a shared repository.

The sensitive strings are **arguments, not constants** - this file must never
itself contain a real username, machine folder name or disk detail.

Usage (Git Bash / bash):

    python tools/scrub_legacy_paths.py HANDOFF.md \\
        --replace "C:\\Users\\<user>\\Music" "C:\\Users\\<you>\\Music" \\
        --replace "E:\\<project-root>" "E:\\<you>\\music-root"

Pass the real values only in your shell command (or a git alias); they end up
in the shell history, not in the repository.
"""
import argparse
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", help="file to rewrite in place")
    parser.add_argument(
        "--replace",
        nargs=2,
        action="append",
        metavar=("OLD", "NEW"),
        default=[],
        required=True,
        help="literal search string and its replacement (repeatable)",
    )
    args = parser.parse_args()

    target = Path(args.path)
    if not target.exists():
        return

    text = target.read_text(encoding="utf-8", errors="surrogateescape")
    original = text
    for old, new in args.replace:
        text = text.replace(old, new)
    if text != original:
        target.write_text(text, encoding="utf-8", errors="surrogateescape")


if __name__ == "__main__":
    main()