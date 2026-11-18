#!/usr/bin/env python3
"""Run pinned Changie commands; wave notes never rewrite package versions."""

from __future__ import annotations

import argparse
import os
import re
import shlex
import subprocess
import tomllib
from pathlib import Path

CHANGIE = "go run github.com/miniscruff/changie@v1.26.0"
WAVE = re.compile(r"v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)")


class ChangelogError(Exception):
    """Invalid or stale generated release notes."""


def changie(root: Path, *args: str) -> str:
    command = shlex.split(os.environ.get("CHANGIE", CHANGIE))
    result = subprocess.run([*command, *args], cwd=root, text=True, capture_output=True)
    if result.returncode:
        raise ChangelogError(result.stderr.strip() or result.stdout.strip())
    return result.stdout


def read_wave(root: Path) -> str:
    versions = []
    for path in (root / ".changes").glob("v*.md"):
        match = WAVE.fullmatch(path.stem)
        if match:
            versions.append((tuple(map(int, match.groups())), path))
    if not versions:
        raise ChangelogError("No Changie wave notes found")
    parts, path = max(versions)
    wave = ".".join(map(str, parts))
    if not path.read_text().startswith(f"## [{wave}] - "):
        raise ChangelogError(f"{path.name}: heading does not match wave {wave}")
    return wave


def merge(root: Path) -> str:
    return changie(root, "merge", "--include-unreleased", "## [Unreleased]", "--dry-run")


def check_changelog(root: Path) -> None:
    if (root / "CHANGELOG.md").read_text() != merge(root):
        raise ChangelogError("CHANGELOG.md differs from generated notes; run make changelog-merge")
    read_wave(root)


def require_batched(root: Path) -> None:
    pending = sorted((root / ".changes/unreleased").rglob("*.yaml"))
    pending += sorted((root / ".changes/unreleased").rglob("*.yml"))
    if pending:
        raise ChangelogError(
            "unreleased fragments must be batched before release: "
            + ", ".join(p.name for p in pending)
        )


def batch_notes(root: Path, version: str) -> None:
    match = WAVE.fullmatch(version)
    if not match:
        raise ChangelogError("WAVE must be X.Y.Z or vX.Y.Z")
    wave = ".".join(match.groups())
    if tuple(map(int, wave.split("."))) <= tuple(map(int, read_wave(root).split("."))):
        raise ChangelogError("The new wave must be newer than the latest generated wave")
    packages = []
    for path in sorted((root / "libs").rglob("pyproject.toml")) + [root / "cli/pyproject.toml"]:
        if "node_modules" in path.parts or ".venv" in path.parts:
            continue
        data = tomllib.loads(path.read_text())["project"]
        packages.append((data["name"], data["version"]))
    import json

    spec = json.loads((root / "libs/spec/package.json").read_text())
    packages.append((spec["name"], spec["version"]))
    header = root / ".changes/unreleased/_release-header.md"
    if header.exists():
        raise ChangelogError(f"Remove stale {header} before batching")
    text = (
        f"This is release wave **v{wave}**, which names the Git tag. Each package\n"
        "versions independently; install the exact package version below, not the wave.\n\n"
        "### Released package versions\n\n"
    )
    text += "\n".join(f"- `{name}` **{version}**" for name, version in sorted(packages)) + "\n"
    header.write_text(text)
    try:
        changie(
            root, "batch", f"v{wave}", "--header-path", header.name, "--allow-no-changes=false"
        )
    finally:
        header.unlink(missing_ok=True)
    (root / "CHANGELOG.md").write_text(merge(root))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["check", "release-check", "merge", "batch", "latest"])
    parser.add_argument("wave", nargs="?")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    try:
        if args.command == "latest":
            print(read_wave(root))
        elif args.command == "merge":
            (root / "CHANGELOG.md").write_text(merge(root))
        elif args.command == "batch":
            batch_notes(root, args.wave or "")
        else:
            check_changelog(root)
            if args.command == "release-check":
                require_batched(root)
            print("Changie notes are current")
    except (ChangelogError, OSError) as exc:
        parser.exit(1, f"changelog: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
