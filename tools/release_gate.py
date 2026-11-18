#!/usr/bin/env python3
"""Fail-closed production tag trust and wave milestone validation."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import tempfile
import tomllib
from pathlib import Path

WAVE = re.compile(r"v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)")


class ReleaseGateError(Exception):
    """A release has not established the required trust or readiness."""


def git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=root, text=True, capture_output=True)
    if result.returncode:
        raise ReleaseGateError(result.stderr.strip() or result.stdout.strip())
    return result.stdout.strip()


def release_mode(event: str, dry_run: str) -> bool:
    if event == "release":
        return False
    if event != "workflow_dispatch" or dry_run not in {"true", "false"}:
        raise ReleaseGateError("Dispatch dry_run must be true or false")
    return dry_run == "true"


def verify_tag(root: Path, tag: str, trusted_ref: str = "origin/main") -> str:
    if not WAVE.fullmatch(tag):
        raise ReleaseGateError("Release tag must be vX.Y.Z")
    tag_ref = f"refs/tags/{tag}"
    if git(root, "cat-file", "-t", tag_ref) != "tag":
        raise ReleaseGateError("Release requires a signed annotated tag")
    commit = git(root, "rev-parse", "--verify", f"{tag_ref}^{{commit}}")
    try:
        git(root, "merge-base", "--is-ancestor", commit, trusted_ref)
    except ReleaseGateError as exc:
        raise ReleaseGateError("Release tag must point to a commit on protected main") from exc
    # Never trust keys from the unverified candidate tag or working tree.
    signers = git(root, "show", f"{trusted_ref}:.github/release-signers")
    if not signers:
        raise ReleaseGateError("The trusted main branch has no release signing keys")
    with tempfile.TemporaryDirectory(prefix="genblaze-tag-trust-") as folder:
        allowed = Path(folder) / "allowed_signers"
        allowed.write_text(signers + "\n")
        git(
            root,
            "-c",
            "gpg.format=ssh",
            "-c",
            "gpg.ssh.program=ssh-keygen",
            "-c",
            f"gpg.ssh.allowedSignersFile={allowed}",
            "verify-tag",
            "--",
            tag_ref,
        )
    return commit


def check_milestone(milestones: list[dict], wave: str) -> None:
    tag = f"v{wave}"
    if not WAVE.fullmatch(tag):
        raise ReleaseGateError("Wave must be X.Y.Z")
    matches = [item for item in milestones if item.get("title") == tag]
    if len(matches) != 1:
        raise ReleaseGateError(f"Expected one release milestone named {tag}")
    milestone = matches[0]
    if milestone.get("state") != "closed" or milestone.get("open_issues") != 0:
        raise ReleaseGateError(f"Milestone {tag} must be closed with no open issues or PRs")


def production(root: Path, tag: str, repository: str) -> None:
    commit = verify_tag(root, tag)
    if commit != git(root, "rev-parse", "HEAD"):
        raise ReleaseGateError("Checked-out candidate differs from the verified release tag")
    result = subprocess.run(
        [
            "gh",
            "api",
            "--paginate",
            "--slurp",
            f"repos/{repository}/milestones?state=all&per_page=100",
        ],
        text=True,
        capture_output=True,
        check=True,
    )
    check_milestone([item for page in json.loads(result.stdout) for item in page], tag[1:])
    print(f"Verified trusted tag {tag} and closed milestone at {commit}")


def write_plan(root: Path) -> None:
    from changelog import read_wave

    source_sha = git(root, "rev-parse", "HEAD")
    workflow_sha = os.environ.get("WORKFLOW_SHA", "")
    workflow_ref = os.environ.get("WORKFLOW_REF", "")
    if workflow_sha and source_sha != workflow_sha:
        raise ReleaseGateError("Candidate must match the workflow commit")
    requested_ref = os.environ.get("INPUT_REF", "")
    if os.environ["EVENT_NAME"] == "workflow_dispatch" and requested_ref:
        prefix = "refs/heads/" if workflow_ref.startswith("refs/heads/") else "refs/tags/"
        current_refs = {
            workflow_sha,
            workflow_ref,
            workflow_ref.removeprefix(prefix),
        }
        if not workflow_sha or requested_ref not in current_refs:
            raise ReleaseGateError(
                "Dispatch ref must match the selected workflow branch/tag or commit; "
                "select the candidate with gh workflow run --ref"
            )
    wave = read_wave(root)
    dry = release_mode(
        os.environ["EVENT_NAME"],
        os.environ.get("INPUT_DRY_RUN", ""),
    )
    tag = (
        os.environ.get("RELEASE_TAG", "") if os.environ["EVENT_NAME"] == "release" else f"v{wave}"
    )
    if not dry:
        if tag != f"v{wave}":
            raise ReleaseGateError("Release tag differs from the latest Changie wave")
        production(root, tag, os.environ["GITHUB_REPOSITORY"])
    values = {
        "wave": wave,
        "source_sha": source_sha,
        "dry_run": str(dry).lower(),
        "upload_python": str(not dry).lower(),
    }
    for key, folder in [("umbrella", "libs/meta"), ("core", "libs/core")]:
        values[key] = tomllib.loads((root / folder / "pyproject.toml").read_text())["project"][
            "version"
        ]
    values["spec"] = json.loads((root / "libs/spec/package.json").read_text())["version"]
    with Path(os.environ["GITHUB_OUTPUT"]).open("a") as stream:
        for key, value in values.items():
            if "\n" in value or "\r" in value:
                raise ReleaseGateError("Invalid multiline release output")
            stream.write(f"{key}={value}\n")
    print("Release plan: " + ", ".join(f"{key}={value}" for key, value in values.items()))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag")
    parser.add_argument("--plan", action="store_true")
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--repository", default="backblaze-labs/genblaze")
    args = parser.parse_args()
    root = args.repo_root.resolve()
    try:
        if args.plan:
            write_plan(root)
        elif args.tag:
            production(root, args.tag, args.repository)
        else:
            raise ReleaseGateError("Supply --plan or --tag")
    except (ReleaseGateError, OSError, subprocess.CalledProcessError, ValueError) as exc:
        parser.exit(1, f"release gate: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
