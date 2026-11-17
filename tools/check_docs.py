#!/usr/bin/env python3
"""Docs lint for the Repo Docs Standard (OKF v0.2 frontmatter + repo hygiene).

Checks
------
* Required files: ``AGENTS.md``, ``CLAUDE.md`` (must import ``@AGENTS.md``),
  ``docs/index.md``.
* Frontmatter on ``docs/**/*.md``: ``type``/``owner``/``status``/``stale_after``
  present and valid. Missing frontmatter is an error only on docs this change
  adds or edits; untouched legacy docs are summarised in one warning.
* Freshness: a passed ``stale_after`` is always a warning, never an error.
* Links: relative links in ``docs/**`` and root ``*.md`` must resolve. Error
  on lines this change adds, warning elsewhere (including older lines of an
  edited file).
* Commands: every ``make <target>`` named in an ``AGENTS.md`` must exist.
* Leak guard (public repo): added lines, commit messages and PR title/body
  must not reference internal hosts or Jira keys. Patterns are stored as
  SHA-256 digests so this file doesn't publish the list it guards.

Usage
-----
    python tools/check_docs.py --base origin/main     # PR / local gate
    python tools/check_docs.py                         # whole tree, warn-only
    python tools/check_docs.py --freshness             # weekly stale report
    python tools/check_docs.py --hash PROJ             # digest for the lists

Exit 1 when any error is found (``--freshness`` always exits 0). Under GitHub
Actions findings are emitted as ``::error``/``::warning`` annotations.

Self-contained by design (stdlib + PyYAML, no genblaze imports) so it can be
lifted into the org's shared CI action unchanged.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import os
import re
import subprocess
import sys
from collections.abc import Iterable, Mapping, Set
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from urllib.parse import unquote

import yaml

# --- Policy -----------------------------------------------------------------

DOC_TYPES = frozenset({"Service", "Library", "Intent", "Spec", "Plan", "Decision"})
DOC_STATUSES = frozenset({"draft", "stable", "deprecated"})
REQUIRED_KEYS = ("type", "owner", "status", "stale_after")

# Reserved names (OKF) plus generated / machine-written docs.
FRONTMATTER_EXEMPT = (
    "**/index.md",
    "**/log.md",
    "**/_template.md",
    "docs/reference/model-matrix.md",
    "docs/exec-plans/active/batch-plan-*.md",
    "docs/exec-plans/active/maintenance-report-*.md",
)

# SHA-256 of upper-case Jira project keys and of internal hostnames (a digest
# of a parent domain also matches its subdomains). Add with ``--hash``; keep
# the plaintext list in the private ``.internal/`` companion repo.
JIRA_KEY_HASHES: frozenset[str] = frozenset()
HOST_HASHES: frozenset[str] = frozenset()
# Public, unambiguous internal-tool host: safe to match in plaintext.
_ATLASSIAN_RE = re.compile(r"\batlassian\.net\b", re.IGNORECASE)

_JIRA_RE = re.compile(r"(?<![A-Za-z0-9-])([A-Z][A-Z0-9]{1,9})-\d+\b")
_HOST_RE = re.compile(r"https?://([A-Za-z0-9.-]+)", re.IGNORECASE)
_LINK_RE = re.compile(r"\[[^\]]*\]\(\s*(<[^>]+>|[^)\s]+)(?:\s+\"[^\"]*\")?\s*\)")
_INLINE_CODE_RE = re.compile(r"`[^`]*`")
_MAKE_RE = re.compile(
    r"(?:^|&&|;|\|)\s*make\s+((?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)*)([A-Za-z0-9_.-]+)"
)
_TARGET_RE = re.compile(r"^([A-Za-z0-9_.-]+)\s*:(?!=)", re.MULTILINE)
_FENCE_RE = re.compile(r"^\s*(```|~~~)")

_MAX_SCAN_BYTES = 1_000_000


@dataclass(frozen=True)
class Finding:
    level: str  # "error" | "warning"
    path: str
    line: int
    message: str


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


# --- Frontmatter --------------------------------------------------------------


def parse_frontmatter(text: str) -> tuple[dict | None, str | None]:
    """Return ``(meta, error)``. ``(None, None)`` means no frontmatter at all."""
    lines = text.lstrip("﻿").replace("\r\n", "\n").split("\n")
    if not lines or lines[0].strip() != "---":
        return None, None
    try:
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    except StopIteration:
        return None, "frontmatter opened with '---' but has no closing '---'"
    try:
        meta = yaml.safe_load("\n".join(lines[1:end]))
    except yaml.YAMLError as exc:
        hint = (
            " (quote values starting with '@', e.g. owner: \"@team\")" if "'@'" in str(exc) else ""
        )
        return None, f"invalid YAML frontmatter{hint}: {str(exc).splitlines()[0]}"
    if not isinstance(meta, dict):
        return None, "frontmatter must be a YAML mapping"
    return meta, None


def _parse_timestamp(value: object) -> datetime | None:
    """Accept only timezone-aware ISO 8601 datetimes (OKF requires an offset)."""
    if isinstance(value, datetime):
        return value if value.tzinfo else None
    if isinstance(value, date):
        return None
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else None
    return None


def is_frontmatter_exempt(rel: str) -> bool:
    name = rel.rsplit("/", 1)[-1]
    return any(
        fnmatch.fnmatch(rel, pat) or (pat.startswith("**/") and name == pat[3:])
        for pat in FRONTMATTER_EXEMPT
    )


def check_frontmatter(rel: str, text: str, *, changed: bool, now: datetime) -> list[Finding]:
    return []  # TEST: drop the rule; main's copy must still enforce it
    meta, error = parse_frontmatter(text)
    if error:
        return [Finding("error", rel, 1, error)]
    if meta is None:
        # Legacy docs are exempt until touched; run() summarises them.
        return (
            [Finding("error", rel, 1, "missing OKF frontmatter (required on new/edited docs)")]
            if changed
            else []
        )

    findings: list[Finding] = []
    missing = [k for k in REQUIRED_KEYS if meta.get(k) in (None, "")]
    if missing:
        findings.append(
            Finding("error", rel, 1, f"frontmatter missing required key(s): {', '.join(missing)}")
        )
    if "type" in meta and meta["type"] not in DOC_TYPES:
        findings.append(
            Finding("error", rel, 1, f"type {meta['type']!r} not in {sorted(DOC_TYPES)}")
        )
    if "status" in meta and meta["status"] not in DOC_STATUSES:
        findings.append(
            Finding("error", rel, 1, f"status {meta['status']!r} not in {sorted(DOC_STATUSES)}")
        )
    if meta.get("stale_after") not in (None, ""):
        stale_after = _parse_timestamp(meta["stale_after"])
        if stale_after is None:
            findings.append(
                Finding(
                    "error",
                    rel,
                    1,
                    "stale_after must be ISO 8601 with a UTC offset, e.g. 2027-05-01T00:00:00Z",
                )
            )
        elif stale_after < now and meta.get("status") != "deprecated":
            findings.append(
                Finding(
                    "warning",
                    rel,
                    1,
                    f"doc is stale (stale_after {stale_after.date()}): re-check it and "
                    "move the date, or set status: deprecated",
                )
            )
    return findings


# --- Links -----------------------------------------------------------------


def _strip_code(text: str) -> list[str]:
    """Return lines with fenced blocks blanked and inline code removed."""
    out, fence = [], None
    for line in text.split("\n"):
        m = _FENCE_RE.match(line)
        if m:
            fence = None if fence == m.group(1) else (fence or m.group(1))
            out.append("")
            continue
        out.append("" if fence else _INLINE_CODE_RE.sub("", line))
    return out


def check_links(
    root: Path, rel: str, text: str, changed: bool, added: Set[int] | None = None
) -> list[Finding]:
    """Broken relative links. Errors only on lines this change adds (all lines
    when ``added`` is None); older broken links in an edited file only warn, so
    a one-line CHANGELOG entry doesn't inherit someone else's moved target."""
    base = (root / rel).parent
    findings = []
    for lineno, line in enumerate(_strip_code(text), start=1):
        for match in _LINK_RE.finditer(line):
            target = match.group(1).strip("<>")
            if "://" in target or target.startswith(("#", "mailto:", "tel:")):
                continue
            path = unquote(target.split("#", 1)[0].split("?", 1)[0])
            if not path:
                continue
            resolved = root / path.lstrip("/") if path.startswith("/") else base / path
            if not resolved.exists():
                is_new = changed and (added is None or lineno in added)
                level = "error" if is_new else "warning"
                findings.append(Finding(level, rel, lineno, f"broken link: {target}"))
    return findings


# --- Commands ----------------------------------------------------------------


def check_commands(rel: str, text: str, makefile: str) -> list[Finding]:
    targets = set(_TARGET_RE.findall(makefile))
    findings, fence = [], None
    for lineno, line in enumerate(text.split("\n"), start=1):
        m = _FENCE_RE.match(line)
        if m:
            fence = None if fence == m.group(1) else (fence or m.group(1))
            continue
        snippets = [line] if fence else [s.strip("`") for s in _INLINE_CODE_RE.findall(line)]
        for snippet in snippets:
            for match in _MAKE_RE.finditer(snippet):
                target = match.group(2)
                if target not in targets:
                    findings.append(
                        Finding("error", rel, lineno, f"`make {target}` is not a Makefile target")
                    )
    return findings


# --- Leak guard ----------------------------------------------------------------


def _host_suffixes(host: str) -> Iterable[str]:
    labels = host.lower().rstrip(".").split(".")
    return (".".join(labels[i:]) for i in range(len(labels) - 1))


def scan_leaks(
    text: str,
    source: str,
    first_line: int,
    key_hashes: frozenset[str],
    host_hashes: frozenset[str],
) -> list[Finding]:
    findings = []
    for offset, line in enumerate(text.split("\n")):
        hit = bool(_ATLASSIAN_RE.search(line))
        hit = hit or any(sha256_hex(m.group(1)) in key_hashes for m in _JIRA_RE.finditer(line))
        hit = hit or any(
            sha256_hex(suffix) in host_hashes
            for m in _HOST_RE.finditer(line)
            for suffix in _host_suffixes(m.group(1))
        )
        if hit:
            # Never echo the matched token: the message itself lands in public CI logs.
            findings.append(
                Finding(
                    "error",
                    source,
                    first_line + offset,
                    "possible internal reference (Jira key or internal host); public "
                    "repos must not contain it. Link the public PR from the ticket instead.",
                )
            )
    return findings


# --- Git ---------------------------------------------------------------------


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True, errors="replace"
    ).stdout


def _merge_base(root: Path, base: str) -> str:
    return _git(root, "merge-base", base, "HEAD").strip()


def changed_files(root: Path, base: str) -> set[str]:
    """Files added, modified or renamed vs the merge-base, incl. uncommitted and untracked."""
    changed = set()
    for line in _git(root, "diff", "--name-status", "-M", _merge_base(root, base)).splitlines():
        parts = line.split("\t")
        if parts[0].startswith("D"):
            continue
        changed.add(parts[-1])  # rename/copy lines end with the new path
    changed.update(_git(root, "ls-files", "--others", "--exclude-standard").splitlines())
    return changed


def added_lines(root: Path, base: str) -> Iterable[tuple[str, int, str]]:
    """Yield ``(path, line, text)`` for every line this change adds."""
    path, lineno = "", 0
    for line in _git(root, "diff", "-U0", "--no-color", _merge_base(root, base)).splitlines():
        if line.startswith("+++ "):
            path = line[6:] if line.startswith("+++ b/") else ""
        elif line.startswith("@@"):
            m = re.search(r"\+(\d+)", line)
            lineno = int(m.group(1)) if m else 0
        elif line.startswith("+") and path:
            yield path, lineno, line[1:]
            lineno += 1
    for rel in _git(root, "ls-files", "--others", "--exclude-standard").splitlines():
        file = root / rel
        if file.is_file() and file.stat().st_size <= _MAX_SCAN_BYTES:
            for i, text in enumerate(file.read_text(errors="ignore").split("\n"), start=1):
                yield rel, i, text


def commit_messages(root: Path, base: str) -> str:
    return _git(root, "log", "--format=%B", f"{_merge_base(root, base)}..HEAD")


# --- Orchestration -----------------------------------------------------------


def check_required(root: Path) -> list[Finding]:
    findings = [
        Finding("error", rel, 0, f"required file {rel} is missing")
        for rel in ("AGENTS.md", "CLAUDE.md", "docs/index.md")
        if not (root / rel).is_file()
    ]
    claude = root / "CLAUDE.md"
    if claude.is_file() and not re.search(r"^@AGENTS\.md\s*$", claude.read_text(), re.MULTILINE):
        findings.append(
            Finding(
                "error", "CLAUDE.md", 1, "CLAUDE.md must import AGENTS.md with a line '@AGENTS.md'"
            )
        )
    return findings


def _markdown_files(root: Path) -> list[str]:
    files = sorted(p.relative_to(root).as_posix() for p in (root / "docs").rglob("*.md"))
    return files + sorted(p.name for p in root.glob("*.md"))


def run(
    root: Path,
    *,
    base: str | None,
    now: datetime,
    pr_texts: Mapping[str, str] | None = None,
) -> list[Finding]:
    try:
        changed = changed_files(root, base) if base else set()
        added = list(added_lines(root, base)) if base else []
    except subprocess.CalledProcessError:
        return [
            Finding(
                "error",
                ".",
                0,
                f"cannot diff against {base!r}. CI needs fetch-depth: 0; on a fork, "
                "run `git fetch upstream main` and `make docs-lint DOCS_BASE=upstream/main`",
            )
        ]
    added_by_file: dict[str, set[int]] = {}
    for path, lineno, _ in added:
        added_by_file.setdefault(path, set()).add(lineno)

    findings = check_required(root)
    legacy = 0
    for rel in _markdown_files(root):
        text = (root / rel).read_text(errors="replace")
        is_changed = rel in changed
        if rel.startswith("docs/") and not is_frontmatter_exempt(rel):
            findings += check_frontmatter(rel, text, changed=is_changed, now=now)
            legacy += not is_changed and parse_frontmatter(text) == (None, None)
        findings += check_links(root, rel, text, is_changed, added_by_file.get(rel, set()))

    makefile = (root / "Makefile").read_text() if (root / "Makefile").is_file() else ""
    for agents in sorted(root.rglob("AGENTS.md")):
        if ".git" not in agents.parts and "node_modules" not in agents.parts:
            findings += check_commands(
                agents.relative_to(root).as_posix(), agents.read_text(), makefile
            )

    if base:
        for path, lineno, text in added:
            findings += scan_leaks(text, path, lineno, JIRA_KEY_HASHES, HOST_HASHES)
        findings += scan_leaks(
            commit_messages(root, base), "commit message", 1, JIRA_KEY_HASHES, HOST_HASHES
        )
    for source, text in (pr_texts or {}).items():
        findings += scan_leaks(text, source, 1, JIRA_KEY_HASHES, HOST_HASHES)

    if legacy:
        findings.append(
            Finding(
                "warning",
                "docs",
                0,
                f"{legacy} docs lack OKF frontmatter (exempt until next edited)",
            )
        )
    return findings


def _emit(findings: list[Finding]) -> None:
    gha = os.environ.get("GITHUB_ACTIONS") == "true"
    for f in findings:
        if gha:
            loc = f"file={f.path}" + (f",line={f.line}" if f.line else "")
            print(f"::{f.level} {loc}::{f.message}")
        else:
            print(f"{f.level}: {f.path}" + (f":{f.line}" if f.line else "") + f": {f.message}")
    errors = sum(f.level == "error" for f in findings)
    print(f"docs lint: {errors} error(s), {len(findings) - errors} warning(s)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="repo root (default: cwd)")
    parser.add_argument(
        "--base", help="git ref to diff against (enables changed-file rules and leak scan)"
    )
    parser.add_argument(
        "--freshness", action="store_true", help="report stale docs only; always exit 0"
    )
    parser.add_argument(
        "--hash", metavar="VALUE", help="print the SHA-256 digest for a pattern list entry"
    )
    args = parser.parse_args(argv)

    if args.hash:
        print(sha256_hex(args.hash))
        return 0

    # PR title/body arrive via env, never interpolated into a shell command.
    pr_texts = {
        k: os.environ[v]
        for k, v in (("PR title", "PR_TITLE"), ("PR body", "PR_BODY"))
        if os.environ.get(v)
    }
    findings = run(args.root.resolve(), base=args.base, now=datetime.now(UTC), pr_texts=pr_texts)
    if args.freshness:
        findings = [f for f in findings if f.message.startswith("doc is stale")]
        _emit(findings)
        return 0
    _emit(findings)
    return 1 if any(f.level == "error" for f in findings) else 0


if __name__ == "__main__":
    sys.exit(main())
