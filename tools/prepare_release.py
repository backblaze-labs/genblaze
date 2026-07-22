#!/usr/bin/env python3
"""
Deterministic wave-level release-prep engine.

Genblaze cuts a "wave" release across ~15 independently-versioned packages
(see RELEASING.md). Preparing one by hand means a human has to remember,
every time: which packages changed since the last tag, bump each one's own
``pyproject.toml``/``package.json`` version, AND keep ``cli``'s and the
``genblaze`` umbrella's dependency floors on ``genblaze-core``/``genblaze-s3``/
every connector in lockstep with whatever those packages' versions actually
are. Missing any one of those is invisible until ``make pypi-pin-parity``
(or worse, the ``pin-parity`` release gate) fails — or, in the case that
motivated this script, a hand-edit silently misses the CLI's core-floor
bump and nobody notices until a much later CI run.

This script makes that bookkeeping mechanical instead of memorized:

* Package set is discovered dynamically (``libs/core``, ``cli``, ``libs/meta``,
  ``libs/spec`` (npm), and a glob over ``libs/connectors/*``) — no hardcoded
  package list or version literal anywhere in this file.
* The reserved ``genblaze-core`` version (the release at which
  ``raise_on_failure``'s default flips — see
  ``_RAISE_ON_FAILURE_DEFAULT_FLIP_VERSION`` in
  ``libs/core/genblaze_core/pipeline/pipeline.py``) is extracted from source
  at runtime and this script refuses to land core on it.
* Dependency floors (``cli``'s and the umbrella's pins on ``genblaze-core``/
  ``genblaze-s3``/every connector) are recomputed from each package's FINAL
  decided version on every run, independent of whether that specific
  package changed this wave — a connectors-only wave still republishes the
  umbrella with the new floors, closing the exact drift class documented in
  ``tools/check_pin_parity.py`` (genblaze-s3 in 0.3.0, genblaze-langsmith +
  genblaze-cli in 0.3.2: a pin widened in source without a version bump,
  silently kept forever by ``skip-existing``).

What this script deliberately does NOT do (left to a human, or to the
``prepare-release`` skill that drives it): pick the wave name, write
CHANGELOG prose, tag, or publish. See ``.claude/skills/prepare-release/SKILL.md``.

Modes
-----
``--check``   Report what would change; exit 0 if nothing is needed, 1 if
              prep is needed, 2 on a hard error (bad args, git failure, or
              a reserved-version collision).
``--apply``   Do the same computation, then write the changes. Exit 0/2.

Overrides
---------
``--set PKG=VERSION``       Force PKG to an exact version instead of the
                             default patch bump.
``--bump PKG=patch|minor|major``  Force a specific bump kind for PKG.

PKG is a package key: ``core``, ``cli``, ``meta``, ``spec``, or a connector
directory name (e.g. ``openai``, ``stability-audio``).

Idempotent by construction: a package is only bumped if its on-disk version
still equals what was published at the last tag (whether or not THIS script
did the bumping) — re-running ``--apply`` after a prior run, or after a
maintainer bumped something by hand, is a safe no-op for that package.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

try:
    import tomllib  # Python 3.11+
except ImportError:  # pragma: no cover - repo requires 3.11+, see AGENTS.md
    print("ERROR: this script needs tomllib (Python 3.11+).", file=sys.stderr)
    sys.exit(2)


class PrepareReleaseError(Exception):
    """Base error for prepare_release failures (bad input, git failure, missing file)."""


class ReservedVersionError(PrepareReleaseError):
    """Raised when a candidate genblaze-core version collides with the reserved
    ``raise_on_failure`` default-flip version."""


# ---------------------------------------------------------------------------
# Package discovery
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PackageInfo:
    """One discovered, independently-versioned package."""

    key: str  # e.g. "core", "cli", "meta", "spec", "openai"
    rel_dir: str  # e.g. "libs/core" — relative to repo root
    manifest_path: str  # e.g. "libs/core/pyproject.toml"
    name: str  # published name, e.g. "genblaze-core", "@genblaze/spec"
    version: str
    kind: str  # "pyproject" | "npm"


def _load_pyproject(path: Path) -> dict:
    with path.open("rb") as f:
        return tomllib.load(f)


def discover_packages(repo_root: Path) -> dict[str, PackageInfo]:
    """Discover every released package. Dynamic — no hardcoded connector list.

    Fixed structural roots (``libs/core``, ``cli``, ``libs/meta``, ``libs/spec``)
    are genuine monorepo layout, not a "list of packages that change"; the
    connector set comes from a glob so a newly scaffolded connector is picked
    up automatically.
    """
    packages: dict[str, PackageInfo] = {}

    for key, rel_dir in (("core", "libs/core"), ("cli", "cli"), ("meta", "libs/meta")):
        manifest_path = f"{rel_dir}/pyproject.toml"
        data = _load_pyproject(repo_root / manifest_path)
        project = data["project"]
        packages[key] = PackageInfo(
            key=key,
            rel_dir=rel_dir,
            manifest_path=manifest_path,
            name=project["name"],
            version=project["version"],
            kind="pyproject",
        )

    connectors_dir = repo_root / "libs/connectors"
    for child in sorted(p for p in connectors_dir.iterdir() if p.is_dir()):
        manifest = child / "pyproject.toml"
        if not manifest.exists():
            continue
        key = child.name
        rel_dir = f"libs/connectors/{key}"
        data = _load_pyproject(manifest)
        project = data["project"]
        packages[key] = PackageInfo(
            key=key,
            rel_dir=rel_dir,
            manifest_path=f"{rel_dir}/pyproject.toml",
            name=project["name"],
            version=project["version"],
            kind="pyproject",
        )

    spec_manifest = "libs/spec/package.json"
    spec_data = json.loads((repo_root / spec_manifest).read_text(encoding="utf-8"))
    packages["spec"] = PackageInfo(
        key="spec",
        rel_dir="libs/spec",
        manifest_path=spec_manifest,
        name=spec_data["name"],
        version=spec_data["version"],
        kind="npm",
    )

    return packages


# ---------------------------------------------------------------------------
# Git plumbing — every call takes repo_root explicitly so tests can point at
# a synthetic fixture repo instead of the live one.
# ---------------------------------------------------------------------------


def _run_git(repo_root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=repo_root, capture_output=True, text=True, check=False
    )


def git_last_tag(repo_root: Path) -> str:
    """Return the most recent ``v*``-matching tag reachable from HEAD."""
    result = _run_git(repo_root, "describe", "--tags", "--match", "v*", "--abbrev=0")
    if result.returncode != 0:
        raise PrepareReleaseError(
            "no prior release tag found (git describe --tags --match 'v*' "
            f"--abbrev=0 failed): {result.stderr.strip()}"
        )
    return result.stdout.strip()


def git_changed_files(repo_root: Path, last_tag: str) -> list[str]:
    """Return paths that differ between ``last_tag`` and HEAD."""
    result = _run_git(repo_root, "diff", "--name-only", f"{last_tag}..HEAD")
    if result.returncode != 0:
        raise PrepareReleaseError(f"git diff {last_tag}..HEAD failed: {result.stderr.strip()}")
    return [line for line in result.stdout.splitlines() if line]


def git_show_file(repo_root: Path, tag: str, rel_path: str) -> str | None:
    """Return the file contents at ``tag``, or None if it didn't exist there
    (a brand-new package added since that tag)."""
    result = _run_git(repo_root, "show", f"{tag}:{rel_path}")
    if result.returncode != 0:
        return None
    return result.stdout


def map_changed_files_to_keys(
    changed_files: list[str], packages: dict[str, PackageInfo]
) -> set[str]:
    """Map changed file paths to the package(s) whose directory contains them.

    Package `rel_dir`s are mutually exclusive prefixes (libs/core, libs/meta,
    libs/connectors/<x>, libs/spec, cli), so a path matches at most one key.
    """
    changed: set[str] = set()
    for f in changed_files:
        for key, pkg in packages.items():
            prefix = pkg.rel_dir.rstrip("/") + "/"
            if f == pkg.rel_dir or f.startswith(prefix):
                changed.add(key)
                break
    return changed


def read_published_versions(
    repo_root: Path, tag: str, packages: dict[str, PackageInfo]
) -> dict[str, str | None]:
    """Return each package's version as published in the manifest at ``tag``,
    or None if the package didn't exist yet (brand new this wave)."""
    published: dict[str, str | None] = {}
    for key, pkg in packages.items():
        raw = git_show_file(repo_root, tag, pkg.manifest_path)
        if raw is None:
            published[key] = None
        elif pkg.kind == "pyproject":
            published[key] = tomllib.loads(raw)["project"]["version"]
        else:
            published[key] = json.loads(raw)["version"]
    return published


# ---------------------------------------------------------------------------
# Version arithmetic
# ---------------------------------------------------------------------------

_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")
_BUMP_KINDS = ("patch", "minor", "major")


def _validate_semver(value: str) -> None:
    if not _SEMVER_RE.match(value):
        raise PrepareReleaseError(f"not a valid X.Y.Z version: {value!r}")


def bump_version(version: str, kind: str) -> str:
    """Bump an X.Y.Z version. ``kind`` is one of patch/minor/major."""
    _validate_semver(version)
    if kind not in _BUMP_KINDS:
        raise PrepareReleaseError(f"unknown bump kind {kind!r}, expected one of {_BUMP_KINDS}")
    major, minor, patch = (int(p) for p in version.split("."))
    if kind == "patch":
        patch += 1
    elif kind == "minor":
        minor += 1
        patch = 0
    else:
        major += 1
        minor = 0
        patch = 0
    return f"{major}.{minor}.{patch}"


# ---------------------------------------------------------------------------
# Reserved core version guard
# ---------------------------------------------------------------------------

_RESERVED_VERSION_RE = re.compile(r'_RAISE_ON_FAILURE_DEFAULT_FLIP_VERSION\s*=\s*"([^"]+)"')
_PIPELINE_MODULE = "libs/core/genblaze_core/pipeline/pipeline.py"


def extract_reserved_core_version(repo_root: Path) -> str:
    """Read the reserved genblaze-core version straight from source — never
    hardcoded here, so this guard can't drift from the actual sentinel."""
    path = repo_root / _PIPELINE_MODULE
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PrepareReleaseError(
            f"could not read {path} to extract the reserved core version: {exc}"
        ) from exc
    match = _RESERVED_VERSION_RE.search(text)
    if not match:
        raise PrepareReleaseError(
            f"could not find _RAISE_ON_FAILURE_DEFAULT_FLIP_VERSION in {path} — "
            "the reserved-version guard cannot run without it."
        )
    return match.group(1)


# ---------------------------------------------------------------------------
# pyproject.toml / package.json text surgery — targeted line rewrites that
# preserve comments and formatting, rather than a full TOML re-serialization.
# ---------------------------------------------------------------------------

_SECTION_RE = re.compile(r"^\[([^\]]+)\]$")
_VERSION_LINE_RE = re.compile(r'^(?P<indent>[ \t]*)version\s*=\s*"(?P<value>[^"]*)"(?P<trail>.*)$')


def set_pyproject_version(text: str, new_version: str) -> str:
    """Replace the `version = "..."` line inside `[project]`, leaving every
    other line (comments, other sections) untouched."""
    out: list[str] = []
    in_project = False
    replaced = False
    for line in text.splitlines(keepends=True):
        stripped = line.rstrip("\n")
        section_match = _SECTION_RE.match(stripped.strip())
        if section_match:
            in_project = section_match.group(1) == "project"
        if in_project and not replaced:
            version_match = _VERSION_LINE_RE.match(stripped)
            if version_match:
                out.append(
                    f'{version_match.group("indent")}version = "{new_version}"'
                    f"{version_match.group('trail')}\n"
                )
                replaced = True
                continue
        out.append(line)
    if not replaced:
        raise PrepareReleaseError('could not find `version = "..."` in [project] table')
    return "".join(out)


_PKG_JSON_VERSION_RE = re.compile(
    r'^(?P<indent>\s*)"version":\s*"(?P<value>[^"]*)"(?P<trail>,?)\s*$'
)


def set_package_json_version(text: str, new_version: str) -> str:
    """Replace the first top-level `"version": "..."` line in a package.json."""
    out: list[str] = []
    replaced = False
    for line in text.splitlines(keepends=True):
        stripped = line.rstrip("\n")
        match = _PKG_JSON_VERSION_RE.match(stripped) if not replaced else None
        if match:
            out.append(
                f'{match.group("indent")}"version": "{new_version}"{match.group("trail")}\n'
            )
            replaced = True
            continue
        out.append(line)
    if not replaced:
        raise PrepareReleaseError('could not find `"version": "..."` in package.json')
    return "".join(out)


# Matches a single quoted dependency specifier *anywhere* on a line — not just
# a line that is nothing but the quoted dep. This is what lets the single-line
# inline-array extras (`decart = ["genblaze-decart>=0.3.1,<0.4"]`) get rewritten
# alongside the multi-line bundle arrays (`video = [\n  "genblaze-decart>=...",\n]`);
# an earlier full-line anchor silently skipped the inline form, leaving umbrella
# per-connector extras stuck on a stale floor every release. Applied per token
# so a line carrying several deps has each one rewritten.
_DEP_TOKEN_RE = re.compile(
    r'"(?P<name>[A-Za-z0-9_.]+(?:-[A-Za-z0-9_.]+)*)'
    r'>=(?P<floor>[0-9][0-9A-Za-z.]*)(?P<rest>[^"]*)"'
)


def rewrite_floors(
    text: str, name_to_key: dict[str, str], final_versions: dict[str, str]
) -> tuple[str, list[tuple[str, str, str]]]:
    """Rewrite every dependency-array entry whose package name is tracked in
    ``name_to_key`` so its floor (the version after ``>=``) matches
    ``final_versions``.

    Only the floor number is substituted — the upper-bound cap, any extras,
    and markers are preserved verbatim from whatever is already in the file,
    so this never hardcodes a version cap. Returns the (possibly) rewritten
    text and a list of ``(dep_name, old_pin, new_pin)`` for lines that
    actually changed.
    """
    changes: list[tuple[str, str, str]] = []

    def _sub(match: re.Match[str]) -> str:
        name = match.group("name")
        key = name_to_key.get(name)
        if key is None or key not in final_versions:
            return match.group(0)
        old_floor = match.group("floor")
        new_floor = final_versions[key]
        if old_floor == new_floor:
            return match.group(0)
        rest = match.group("rest")
        changes.append((name, f"{name}>={old_floor}{rest}", f"{name}>={new_floor}{rest}"))
        return f'"{name}>={new_floor}{rest}"'

    return _DEP_TOKEN_RE.sub(_sub, text), changes


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------


@dataclass
class VersionChange:
    key: str
    name: str
    old: str
    new: str
    reason: str


@dataclass
class FloorChange:
    file_key: str  # "cli" or "meta"
    dep_name: str
    old_pin: str
    new_pin: str


@dataclass
class Plan:
    last_tag: str
    changed_keys: set[str]
    version_changes: dict[str, VersionChange]
    floor_changes: list[FloorChange]
    warnings: list[str]
    released_versions_text: str
    final_versions: dict[str, str]
    packages: dict[str, PackageInfo] = field(repr=False)
    prep_needed: bool


_PRIORITY = {"meta": 0, "core": 1, "cli": 2, "s3": 3}


def _released_versions_sort_key(key: str) -> tuple[int, str]:
    if key == "spec":
        return (99, key)
    return (_PRIORITY.get(key, 50), key)


def render_released_versions(
    packages: dict[str, PackageInfo],
    published: dict[str, str | None],
    changes: dict[str, VersionChange],
) -> str:
    """Render the "### Released package versions" list for the human to
    paste into the CHANGELOG wave header. Deliberately does not invent
    change-summary prose — just the version transitions."""
    lines = ["### Released package versions", ""]
    for key in sorted(changes, key=_released_versions_sort_key):
        change = changes[key]
        pkg = packages[key]
        label = f"`{pkg.name}` (umbrella)" if key == "meta" else f"`{pkg.name}`"
        old_label = published[key] or "new"
        lines.append(f"- {label} {old_label} → **{change.new}**")
    if len(lines) == 2:
        lines.append("_(nothing changed this wave)_")
    return "\n".join(lines)


def parse_overrides(pairs: list[str], flag: str) -> dict[str, str]:
    """Parse repeated ``PKG=VALUE`` CLI args into a dict, validating shape."""
    result: dict[str, str] = {}
    for pair in pairs:
        if "=" not in pair:
            raise PrepareReleaseError(f"invalid --{flag} value {pair!r}, expected PKG=VALUE")
        key, value = pair.split("=", 1)
        key, value = key.strip(), value.strip()
        if not key or not value:
            raise PrepareReleaseError(f"invalid --{flag} value {pair!r}, expected PKG=VALUE")
        if flag == "bump" and value not in _BUMP_KINDS:
            raise PrepareReleaseError(
                f"invalid --bump kind {value!r} for {key!r}, expected one of {_BUMP_KINDS}"
            )
        result[key] = value
    return result


def build_plan(
    repo_root: Path,
    *,
    set_overrides: dict[str, str] | None = None,
    bump_overrides: dict[str, str] | None = None,
) -> Plan:
    """Compute the full release-prep plan. Raises PrepareReleaseError (or the
    ReservedVersionError subclass) on any hard error; never partially applies
    anything — this function only computes, `apply_plan` writes."""
    set_overrides = dict(set_overrides or {})
    bump_overrides = dict(bump_overrides or {})

    packages = discover_packages(repo_root)
    unknown = (set(set_overrides) | set(bump_overrides)) - set(packages)
    if unknown:
        raise PrepareReleaseError(
            f"unknown package(s) in --set/--bump: {', '.join(sorted(unknown))} "
            f"(known: {', '.join(sorted(packages))})"
        )
    for value in set_overrides.values():
        _validate_semver(value)

    last_tag = git_last_tag(repo_root)
    changed_files = git_changed_files(repo_root, last_tag)
    changed_keys = map_changed_files_to_keys(changed_files, packages)
    published = read_published_versions(repo_root, last_tag, packages)

    changes: dict[str, VersionChange] = {}
    bumped: set[str] = set()

    def _decide(key: str, candidate: str, reason: str) -> None:
        pkg = packages[key]
        if candidate != pkg.version:
            changes[key] = VersionChange(key, pkg.name, pkg.version, candidate, reason)
        bumped.add(key)

    # 1. Explicit --set overrides — highest priority, and locks the package
    #    from any further auto-bump logic below even if the value equals
    #    what's already on disk (an explicit no-op override still counts as
    #    "handled" for this run).
    for key, value in set_overrides.items():
        _decide(key, value, "explicit --set override")

    # 2. Explicit --bump overrides.
    for key, kind in bump_overrides.items():
        if key in bumped:
            continue
        _decide(key, bump_version(packages[key].version, kind), f"explicit --bump {kind}")

    # 3. Default patch bump for packages that changed since the last tag and
    #    haven't already moved past what was published there — this is what
    #    makes --apply safe to re-run (a package already bumped, whether by
    #    a prior run of this tool or a hand-edit, is left alone). A package
    #    with no published version at all (pub is None) is brand new this
    #    wave — its initial version is whatever the scaffold set, not this
    #    tool's business to bump.
    for key in sorted(changed_keys):
        if key in bumped:
            continue
        pkg = packages[key]
        pub = published[key]
        if pub is None or pkg.version != pub:
            continue  # brand new, or already bumped since the tag
        _decide(key, bump_version(pkg.version, "patch"), f"code changed since {last_tag}")

    final_versions: dict[str, str] = {
        key: (changes[key].new if key in changes else pkg.version) for key, pkg in packages.items()
    }

    # 4. Reserved-version guard, checked against the FINAL core version
    #    regardless of how it got there — a hand-edit that lands core on the
    #    reserved value can't slip past `--check` just because this tool
    #    itself decided not to touch it.
    reserved = extract_reserved_core_version(repo_root)
    if final_versions["core"] == reserved:
        raise ReservedVersionError(
            f"genblaze-core version {reserved} is reserved (see "
            f"_RAISE_ON_FAILURE_DEFAULT_FLIP_VERSION in {_PIPELINE_MODULE} — "
            "raise_on_failure's default flips there). Refusing to prepare a "
            "release that lands core on this version; pick a different "
            "version explicitly with --set core=<version> if this is "
            "intentional."
        )

    # 5. Floor sync — cli's and the umbrella's genblaze-core/-s3/-<connector>
    #    pins must always match the FINAL version of the package they point
    #    at, whether or not that package changed this wave. A pin that would
    #    change without its own version bumping is the exact skip-existing
    #    trap this tool exists to prevent, so force a default bump too.
    name_to_key = {pkg.name: key for key, pkg in packages.items() if pkg.kind == "pyproject"}
    floor_changes: list[FloorChange] = []
    for file_key in ("cli", "meta"):
        pkg = packages[file_key]
        text = (repo_root / pkg.manifest_path).read_text(encoding="utf-8")
        _new_text, dep_changes = rewrite_floors(text, name_to_key, final_versions)
        if not dep_changes:
            continue
        floor_changes.extend(
            FloorChange(file_key, name, old_pin, new_pin) for name, old_pin, new_pin in dep_changes
        )
        if file_key not in bumped:
            # A connector referenced in multiple extras (e.g. an individual
            # extra plus the video/image/audio/all bundles) produces one
            # dep_changes entry per line — dedupe for a readable reason string.
            dep_list = ", ".join(dict.fromkeys(name for name, _, _ in dep_changes))
            _decide(
                file_key,
                bump_version(pkg.version, "patch"),
                f"dependency floor updated: {dep_list}",
            )
            final_versions[file_key] = changes[file_key].new

    # 6. npm/spec reminder.
    warnings: list[str] = []
    if "spec" in changes:
        warnings.append(
            "libs/spec changed this wave — run `make ts-types` and commit the "
            "regenerated libs/spec/ts/genblaze.d.ts before tagging."
        )

    # 7. New/unwired connector warning — a connector never referenced in the
    #    umbrella's optional-dependencies is missing its extra + bundle
    #    membership, which is an editorial call this tool won't guess at.
    meta_text = (repo_root / packages["meta"].manifest_path).read_text(encoding="utf-8")
    for key, pkg in packages.items():
        if key in ("core", "cli", "meta", "spec"):
            continue
        if pkg.name not in meta_text:
            warnings.append(
                f"{pkg.name} has no entry anywhere in libs/meta/pyproject.toml — "
                "add its extra (and bundle membership) manually."
            )

    released_versions_text = render_released_versions(packages, published, changes)

    return Plan(
        last_tag=last_tag,
        changed_keys=changed_keys,
        version_changes=changes,
        floor_changes=floor_changes,
        warnings=warnings,
        released_versions_text=released_versions_text,
        final_versions=final_versions,
        packages=packages,
        prep_needed=bool(changes) or bool(floor_changes),
    )


def apply_plan(repo_root: Path, plan: Plan) -> None:
    """Write every version bump and floor update computed in `plan`."""
    packages = plan.packages

    for key, change in plan.version_changes.items():
        pkg = packages[key]
        path = repo_root / pkg.manifest_path
        text = path.read_text(encoding="utf-8")
        if pkg.kind == "pyproject":
            text = set_pyproject_version(text, change.new)
        else:
            text = set_package_json_version(text, change.new)
        path.write_text(text, encoding="utf-8")

    name_to_key = {pkg.name: key for key, pkg in packages.items() if pkg.kind == "pyproject"}
    for file_key in ("cli", "meta"):
        pkg = packages[file_key]
        path = repo_root / pkg.manifest_path
        text = path.read_text(encoding="utf-8")
        new_text, _changes = rewrite_floors(text, name_to_key, plan.final_versions)
        if new_text != text:
            path.write_text(new_text, encoding="utf-8")


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def render_report(plan: Plan) -> str:
    lines = [f"Prepare release — changes since {plan.last_tag}", ""]

    if plan.changed_keys:
        lines.append(f"Changed packages (git diff since {plan.last_tag}):")
        lines.append("  " + ", ".join(sorted(plan.changed_keys)))
    else:
        lines.append(f"No package directories changed since {plan.last_tag}.")
    lines.append("")

    if plan.version_changes:
        lines.append("Version bumps:")
        for key in sorted(plan.version_changes, key=_released_versions_sort_key):
            change = plan.version_changes[key]
            lines.append(f"  {change.name:<28} {change.old} -> {change.new}   ({change.reason})")
    else:
        lines.append("Version bumps: none needed.")
    lines.append("")

    if plan.floor_changes:
        lines.append("Dependency floor updates:")
        for fc in plan.floor_changes:
            manifest = plan.packages[fc.file_key].manifest_path
            lines.append(f"  {manifest}: {fc.old_pin} -> {fc.new_pin}")
    else:
        lines.append("Dependency floor updates: none needed.")
    lines.append("")

    if plan.warnings:
        lines.append("Warnings:")
        for warning in plan.warnings:
            lines.append(f"  - {warning}")
        lines.append("")

    lines.append(plan.released_versions_text)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Deterministic wave-level release-prep engine for genblaze."
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--check",
        action="store_true",
        help="Report what would change. Exit 0 if nothing is needed, 1 if prep is needed.",
    )
    mode.add_argument(
        "--apply",
        action="store_true",
        help="Write the computed version bumps and dependency floor updates.",
    )
    parser.add_argument(
        "--set",
        action="append",
        default=[],
        metavar="PKG=VERSION",
        help="Force PKG to an exact version instead of the default patch bump.",
    )
    parser.add_argument(
        "--bump",
        action="append",
        default=[],
        metavar="PKG=patch|minor|major",
        help="Force a specific bump kind for PKG.",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=None,
        help="Repository root (default: parent of tools/).",
    )
    args = parser.parse_args(argv)

    repo_root = args.repo_root or Path(__file__).resolve().parent.parent

    try:
        set_overrides = parse_overrides(args.set, "set")
        bump_overrides = parse_overrides(args.bump, "bump")
        plan = build_plan(repo_root, set_overrides=set_overrides, bump_overrides=bump_overrides)
    except PrepareReleaseError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    print(render_report(plan))

    if args.check:
        return 1 if plan.prep_needed else 0

    if not plan.prep_needed:
        print()
        print("Nothing to prepare — every package is already up to date for this wave.")
        return 0

    apply_plan(repo_root, plan)
    print()
    print(
        f"Applied {len(plan.version_changes)} version bump(s) and "
        f"{len(plan.floor_changes)} floor update(s)."
    )
    print()
    print(
        "Next: paste the released-versions list above into CHANGELOG.md's new "
        "wave heading, then run `make pre-release`. See RELEASING.md."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
