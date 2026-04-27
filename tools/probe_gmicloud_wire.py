#!/usr/bin/env python3
"""Live-API wire-conformance probe for GMICloud — settles three drift claims.

Sample-app feedback (2026-04-25, items F-2026-04-25-08, -12, -13) reported
that the SDK's registered wire-keys, slug case, and parameter coercers don't
match what the live GMICloud request-queue accepts. This probe runs a small
matrix against the live ``POST /requests`` endpoint to either confirm the
report and trigger spec updates, or refute it and close the items.

**This script makes real network requests** against the GMICloud request
queue. Each request submits a *minimal* payload — just enough to elicit the
acceptance/rejection signal we need. Submissions that get accepted will
return a request id; those tasks should be cancelled or left to time out
upstream. We never poll for completion.

Usage::

    export GMI_API_KEY="gmi-..."
    python tools/probe_gmicloud_wire.py

    # Probe a subset:
    python tools/probe_gmicloud_wire.py --skip-i2v
    python tools/probe_gmicloud_wire.py --models kling-image2video-v2.1-master

Outputs:

- ``docs/reference/gmicloud-wire-probe-{date}.json`` — machine-readable
- ``docs/reference/gmicloud-wire-probe-{date}.md`` — human-readable summary

Probe matrix:

- **Slug case** (F-2026-04-25-13): each of 7 named families is submitted with
  both its lowercase canonical and its PascalCase deprecated alias. We
  record which one(s) the upstream accepts.
- **Per-model image wire-key** (F-2026-04-25-12): each i2v variant
  (``kling-image2video-v2.1-master``, ``wan2.6-i2v``, ``pixverse-v5.6-i2v``)
  is submitted with each of three candidate keys (``image``, ``img_url``,
  ``image_url``). We use a fixed public test image URL.
- **PixVerse duration coercer** (F-2026-04-25-08): ``pixverse-v5.6-i2v`` is
  submitted with ``duration=5`` (int) and ``duration="5"`` (string).

A "200" or "202" with a request id means accepted. "400" with a parameter
error means rejected. We capture the error body excerpt so the report
distinguishes "unknown parameter" from "invalid value".
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import OrderedDict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_BASE_URL = "https://console.gmicloud.ai/api/v1/ie/requestqueue/apikey"
_TEST_IMAGE_URL = (
    # Public-domain test image. Replace with a B2 object if this URL goes
    # away — any reachable HTTPS image works for an "is this param accepted"
    # probe.
    "https://upload.wikimedia.org/wikipedia/commons/thumb/3/3a/Cat03.jpg/320px-Cat03.jpg"
)

# 7 families flagged in F-2026-04-25-13. Tuples of (canonical_lowercase,
# pascal_alias). The probe submits each casing and records whether GMICloud
# accepts it.
_SLUG_CASE_FAMILIES: list[tuple[str, str]] = [
    ("kling-image2video-v2.1-master", "Kling-Image2Video-V2.1-Master"),
    ("kling-image2video-v2.1-pro", "Kling-Image2Video-V2.1-Pro"),
    ("kling-text2video-v2.1-master", "Kling-Text2Video-V2.1-Master"),
    ("kling-text2video-v2.1-pro", "Kling-Text2Video-V2.1-Pro"),
    ("veo3", "Veo3"),
    ("veo3-fast", "Veo3-Fast"),
    ("sora-2-pro", "Sora-2-Pro"),
    ("luma-ray-2", "Luma-Ray-2"),
    ("minimax-hailuo-2.3-fast", "Minimax-Hailuo-2.3-Fast"),
]

# 3 i2v models flagged in F-2026-04-25-12. Each gets submitted with each of
# the three candidate image-key names.
_IMAGE_KEY_TARGETS: list[str] = [
    "kling-image2video-v2.1-master",
    "wan2.6-i2v",
    "pixverse-v5.6-i2v",
]
_IMAGE_KEY_CANDIDATES: list[str] = ["image", "img_url", "image_url"]

# F-2026-04-25-08: PixVerse duration coercer.
_PIXVERSE_DURATION_TARGETS: list[str] = [
    "pixverse-v5.6-t2v",
    "pixverse-v5.6-i2v",
    "pixverse-v5.6-transition",
]
_DURATION_CANDIDATES: list[Any] = [5, "5"]


def _api_key() -> str:
    key = os.environ.get("GMI_API_KEY")
    if not key:
        sys.exit(
            "Set GMI_API_KEY to the GMICloud staging or prod API key before running this probe."
        )
    return key


def _post_minimal(
    client: httpx.Client,
    model_id: str,
    payload: dict[str, Any],
) -> tuple[int, str]:
    """Submit a minimal payload to ``/requests``; return (status, body excerpt).

    Body excerpt is truncated to 400 chars so the report stays scannable.
    """
    try:
        resp = client.post(
            "/requests",
            json={"model": model_id, "payload": payload},
            timeout=15.0,
        )
    except httpx.HTTPError as exc:
        return (0, f"<httpx error: {type(exc).__name__}: {exc}>")
    body = resp.text or ""
    if len(body) > 400:
        body = body[:400] + "...(truncated)"
    return (resp.status_code, body)


def _probe_slug_case(client: httpx.Client) -> list[dict[str, Any]]:
    """For each named family, submit with both casings; record which upstream accepts."""
    rows: list[dict[str, Any]] = []
    for canonical, pascal in _SLUG_CASE_FAMILIES:
        # Use a minimal text-prompt payload — enough that we get past
        # "missing prompt" validation and into "is this slug recognized".
        payload = {"prompt": "wire-conformance probe — minimal submission"}
        status_lower, body_lower = _post_minimal(client, canonical, payload)
        status_pascal, body_pascal = _post_minimal(client, pascal, payload)
        rows.append(
            {
                "family": canonical,
                "lowercase": {"slug": canonical, "status": status_lower, "body": body_lower},
                "pascal": {"slug": pascal, "status": status_pascal, "body": body_pascal},
                "verdict": _classify_slug_verdict(status_lower, status_pascal),
            }
        )
    return rows


def _classify_slug_verdict(lower: int, pascal: int) -> str:
    """Map a (lower_status, pascal_status) pair to a coarse verdict."""
    accepts = lambda s: s in (200, 201, 202)  # noqa: E731
    if accepts(lower) and accepts(pascal):
        return "BOTH_ACCEPTED"
    if accepts(lower) and not accepts(pascal):
        return "LOWERCASE_ONLY (current SDK direction is correct)"
    if accepts(pascal) and not accepts(lower):
        return "PASCAL_ONLY (R-06 was wrong for this family — flip canonical)"
    return "BOTH_REJECTED (unrelated failure — investigate body excerpts)"


def _probe_image_keys(client: httpx.Client) -> list[dict[str, Any]]:
    """For each i2v model, try each candidate image key; record which is accepted."""
    rows: list[dict[str, Any]] = []
    for model in _IMAGE_KEY_TARGETS:
        per_key: dict[str, Any] = OrderedDict()
        for key in _IMAGE_KEY_CANDIDATES:
            payload: dict[str, Any] = {
                "prompt": "wire-conformance probe",
                key: _TEST_IMAGE_URL,
            }
            status, body = _post_minimal(client, model, payload)
            per_key[key] = {"status": status, "body": body}
        rows.append(
            {
                "model": model,
                "candidates": per_key,
                "accepted_keys": [k for k, v in per_key.items() if v["status"] in (200, 201, 202)],
            }
        )
    return rows


def _probe_pixverse_duration(client: httpx.Client) -> list[dict[str, Any]]:
    """Submit each PixVerse model with duration=5 (int) and duration='5' (string)."""
    rows: list[dict[str, Any]] = []
    for model in _PIXVERSE_DURATION_TARGETS:
        per_type: dict[str, Any] = OrderedDict()
        for value in _DURATION_CANDIDATES:
            payload: dict[str, Any] = {
                "prompt": "wire-conformance probe",
                "duration": value,
                "quality": "720p",  # required for PixVerse — avoid masking the duration check
            }
            # i2v needs an image; t2v doesn't; transition is image-pair-based.
            # Add image when applicable so we don't fail on missing-required.
            if "i2v" in model:
                payload["image"] = _TEST_IMAGE_URL
            status, body = _post_minimal(client, model, payload)
            per_type[type(value).__name__] = {
                "value": value,
                "status": status,
                "body": body,
            }
        rows.append({"model": model, "by_type": per_type})
    return rows


def _build_markdown_report(report: dict[str, Any]) -> str:
    """Render the JSON report as a human-readable markdown summary."""
    lines: list[str] = []
    lines.append("<!-- generated by tools/probe_gmicloud_wire.py — do not hand-edit -->")
    lines.append(f"# GMICloud wire-conformance probe — {report['probed_at']}")
    lines.append("")
    lines.append(
        "Settles F-2026-04-25-08 (PixVerse `duration` enum), -12 (per-model "
        "image wire-key), -13 (slug case for 7 named families). See "
        "[`docs/exec-plans/active/gmi-registry-reconciliation.md`]"
        "(../exec-plans/active/gmi-registry-reconciliation.md) for the "
        "resolution paths."
    )
    lines.append("")

    lines.append("## Slug case (F-2026-04-25-13)")
    lines.append("")
    lines.append("| Family | Lowercase | PascalCase | Verdict |")
    lines.append("|---|---|---|---|")
    for row in report["slug_case"]:
        lines.append(
            f"| `{row['family']}` "
            f"| {row['lowercase']['status']} "
            f"| {row['pascal']['status']} "
            f"| **{row['verdict']}** |"
        )
    lines.append("")

    lines.append("## Per-model image wire-key (F-2026-04-25-12)")
    lines.append("")
    lines.append("| Model | `image` | `img_url` | `image_url` | Accepted keys |")
    lines.append("|---|---|---|---|---|")
    for row in report["image_keys"]:
        c = row["candidates"]
        accepted = ", ".join(f"`{k}`" for k in row["accepted_keys"]) or "none"
        lines.append(
            f"| `{row['model']}` "
            f"| {c['image']['status']} "
            f"| {c['img_url']['status']} "
            f"| {c['image_url']['status']} "
            f"| {accepted} |"
        )
    lines.append("")

    lines.append("## PixVerse `duration` coercer (F-2026-04-25-08)")
    lines.append("")
    lines.append('| Model | `duration=5` (int) | `duration="5"` (str) |')
    lines.append("|---|---|---|")
    for row in report["pixverse_duration"]:
        bt = row["by_type"]
        lines.append(f"| `{row['model']}` | {bt['int']['status']} | {bt['str']['status']} |")
    lines.append("")

    lines.append("## Raw response excerpts")
    lines.append("")
    lines.append(
        "Full response bodies (truncated to 400 chars) are in the JSON "
        "report; this markdown summary keeps the table scannable. When a "
        "verdict is surprising, dig into the JSON for the actual error "
        "messages."
    )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "--out-json",
        type=Path,
        help="Path to write the JSON report (default: docs/reference/gmicloud-wire-probe-{date}.json)",
    )
    parser.add_argument(
        "--out-md",
        type=Path,
        help="Path to write the markdown report (default: docs/reference/gmicloud-wire-probe-{date}.md)",
    )
    parser.add_argument("--skip-slug-case", action="store_true")
    parser.add_argument("--skip-i2v", action="store_true", help="Skip image-wire-key probes")
    parser.add_argument(
        "--skip-duration", action="store_true", help="Skip PixVerse duration probe"
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("GMI_BASE_URL") or _DEFAULT_BASE_URL,
        help="Override the request-queue base URL (default: %(default)s)",
    )
    args = parser.parse_args()

    api_key = _api_key()
    today = datetime.now(UTC).date().isoformat()
    out_json = (
        args.out_json or _REPO_ROOT / "docs" / "reference" / f"gmicloud-wire-probe-{today}.json"
    )
    out_md = args.out_md or _REPO_ROOT / "docs" / "reference" / f"gmicloud-wire-probe-{today}.md"

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    report: dict[str, Any] = {
        "probed_at": datetime.now(UTC).isoformat(),
        "base_url": args.base_url,
        "slug_case": [],
        "image_keys": [],
        "pixverse_duration": [],
    }

    with httpx.Client(base_url=args.base_url, headers=headers) as client:
        if not args.skip_slug_case:
            print("Probing slug case for 9 families...", file=sys.stderr)
            report["slug_case"] = _probe_slug_case(client)
        if not args.skip_i2v:
            print(
                f"Probing image wire-keys for {len(_IMAGE_KEY_TARGETS)} i2v models...",
                file=sys.stderr,
            )
            report["image_keys"] = _probe_image_keys(client)
        if not args.skip_duration:
            print(
                f"Probing PixVerse duration coercer for {len(_PIXVERSE_DURATION_TARGETS)} models...",
                file=sys.stderr,
            )
            report["pixverse_duration"] = _probe_pixverse_duration(client)

    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(report, indent=2))
    out_md.write_text(_build_markdown_report(report))
    print(f"Wrote {out_json.relative_to(_REPO_ROOT)}", file=sys.stderr)
    print(f"Wrote {out_md.relative_to(_REPO_ROOT)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
