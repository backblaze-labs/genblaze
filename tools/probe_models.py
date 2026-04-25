#!/usr/bin/env python3
"""Live-API model probe — guards the registry against drift.

Walks every entry-point-registered provider, calls ``probe_model`` for every
default ``model_id`` exposed by ``models_default()``, and writes a JSON
report. Exit code 1 on any ``NOT_FOUND`` result so CI can fail the release.

Usage::

    # Probe every installed provider (requires GENBLAZE_PROBE_<NAME>_API_KEY
    # env vars per provider).
    python tools/probe_models.py

    # Probe a single provider.
    python tools/probe_models.py --provider gmicloud-video

    # Write the report somewhere other than the default.
    python tools/probe_models.py --out docs/reference/model-probe-status.json

The report shape is::

    {
      "generated_at": "2026-04-24T18:30:00Z",
      "providers": {
        "gmicloud-video": {
          "models": {
            "seedance-1-0-pro-250528": {"status": "ok", "detail": "..."},
            "veo3-fast": {"status": "not_found", "detail": "..."}
          },
          "summary": {"ok": 18, "not_found": 1, "auth": 0, "skipped": 0, "unknown": 0}
        }
      }
    }
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from genblaze_core.providers import (
    ProbeResult,
    ProbeStatus,
    discover_providers,
    instantiate_with_credential,
)
from genblaze_core.providers.base import BaseProvider

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_OUT = _REPO_ROOT / "docs" / "reference" / "model-probe-status.json"

# Mapping from provider name → env var with the probe credential. Defaults
# to ``GENBLAZE_PROBE_<UPPER>_API_KEY``; override here only when the upstream
# uses a different credential parameter name.
_CREDENTIAL_ENV_OVERRIDES: dict[str, str] = {}


def _env_var_for(provider_name: str) -> str:
    if provider_name in _CREDENTIAL_ENV_OVERRIDES:
        return _CREDENTIAL_ENV_OVERRIDES[provider_name]
    sanitized = provider_name.upper().replace("-", "_")
    return f"GENBLAZE_PROBE_{sanitized}_API_KEY"


def _probe_provider(name: str, cls: type[BaseProvider]) -> dict[str, Any]:
    api_key = os.environ.get(_env_var_for(name))
    if not api_key:
        return {
            "models": {},
            "summary": {"skipped": len(cls.models_default().known())},
            "skipped_reason": f"missing env var {_env_var_for(name)}",
        }
    provider = instantiate_with_credential(cls, api_key)
    rows: dict[str, dict[str, Any]] = {}
    counts: Counter[str] = Counter()
    for model_id in provider.models.known():
        result = provider.probe_model(model_id)
        if not isinstance(result, ProbeResult):
            result = ProbeResult.unknown(detail=f"non-ProbeResult return: {result!r}")
        rows[model_id] = {"status": result.status.value, "detail": result.detail}
        counts[result.status.value] += 1
    return {"models": rows, "summary": dict(counts)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Probe registered model ids against live APIs.")
    parser.add_argument(
        "--provider",
        action="append",
        help="Probe only this provider (repeatable). Defaults to every discovered provider.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=_DEFAULT_OUT,
        help=f"Write report JSON to this path (default: {_DEFAULT_OUT.relative_to(_REPO_ROOT)}).",
    )
    parser.add_argument(
        "--allow-not-found",
        action="store_true",
        help="Don't exit non-zero on NOT_FOUND. Use for ad-hoc local runs.",
    )
    args = parser.parse_args(argv)

    discovered = discover_providers()
    if args.provider:
        missing = [p for p in args.provider if p not in discovered]
        if missing:
            print(f"Unknown provider(s): {missing}", file=sys.stderr)
            return 2
        discovered = {k: v for k, v in discovered.items() if k in args.provider}

    report: dict[str, Any] = {
        "generated_at": datetime.now(UTC).isoformat(),
        "providers": {},
    }
    not_found_total = 0
    for name in sorted(discovered):
        cls = discovered[name]
        result = _probe_provider(name, cls)
        report["providers"][name] = result
        not_found_total += result["summary"].get(ProbeStatus.NOT_FOUND.value, 0)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(f"Wrote {args.out.relative_to(_REPO_ROOT)} ({len(report['providers'])} providers).")

    if not_found_total and not args.allow_not_found:
        print(
            f"FAIL: {not_found_total} default model id(s) returned NOT_FOUND. "
            "See report for details.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
