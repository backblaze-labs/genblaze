#!/usr/bin/env python3
"""End-to-end smoke: the user flow, through the installed ``genblaze`` CLI.

Unit tests prove the pieces; this proves the product works when they're put
together the way a user does it:

1. run a ``Pipeline`` (mock provider, no network) that produces a real PNG
2. save it with the provenance manifest embedded
3. ``genblaze extract`` the manifest back out and compare its hash
4. ``genblaze verify --fetch`` it, re-hashing the generated asset bytes
5. tamper with the asset and confirm ``verify --fetch`` now fails, so the
   smoke can't pass vacuously

Exits non-zero with a ``FAIL:`` line on the first broken step. Deterministic,
offline, a few seconds. Run via ``make e2e-smoke`` (part of ``make verify``).
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from genblaze_core.mocks import MockProvider
from genblaze_core.models.asset import Asset
from genblaze_core.models.enums import RunStatus
from genblaze_core.pipeline import Pipeline
from PIL import Image


class SmokeFailure(Exception):
    pass


def _cli(*args: str | Path) -> subprocess.CompletedProcess[str]:
    exe = shutil.which("genblaze")
    if exe is None:
        raise SmokeFailure("`genblaze` CLI not on PATH; run `make install-dev`")
    return subprocess.run([exe, *map(str, args)], capture_output=True, text=True, timeout=60)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_smoke(workdir: Path) -> None:
    # 1. Generate: the mock provider "returns" a real PNG on disk.
    generated = workdir / "generated.png"
    Image.new("RGB", (8, 8), color="red").save(generated)
    asset = Asset(url=generated.as_uri(), media_type="image/png", sha256=_sha256(generated))
    result = (
        Pipeline("e2e-smoke")
        .step(MockProvider(assets=[asset]), model="mock/image", prompt="a red square")
        .run(raise_on_failure=True)
    )
    if result.run.status != RunStatus.COMPLETED:
        raise SmokeFailure(f"pipeline status {result.run.status}, expected COMPLETED")

    # 2. Save: embed the manifest into a delivered copy (embedding changes its
    #    bytes, so the generated asset stays the sha256-addressed original).
    delivered = workdir / "delivered.png"
    shutil.copy(generated, delivered)
    result.save(delivered)

    # 3. Extract through the CLI.
    proc = _cli("extract", delivered)
    if proc.returncode != 0:
        raise SmokeFailure(f"genblaze extract exited {proc.returncode}: {proc.stderr.strip()}")
    extracted_hash = json.loads(proc.stdout).get("canonical_hash")
    if extracted_hash != result.manifest.canonical_hash:
        raise SmokeFailure("extracted canonical_hash does not match the pipeline's manifest")

    # 4. Verify through the CLI, including asset bytes.
    proc = _cli("verify", "--fetch", "--allowed-root", workdir, delivered)
    if proc.returncode != 0:
        raise SmokeFailure(
            f"genblaze verify --fetch exited {proc.returncode}: {proc.stderr.strip()}"
        )

    # 5. Negative control: a tampered asset must fail verification.
    generated.write_bytes(generated.read_bytes() + b"tampered")
    proc = _cli("verify", "--fetch", "--allowed-root", workdir, delivered)
    if proc.returncode == 0:
        raise SmokeFailure("genblaze verify --fetch passed on a tampered asset")
    if "sha256" not in proc.stderr:
        # Fails for the right reason, not e.g. a crash or a path-policy refusal.
        raise SmokeFailure(f"tamper check failed for an unexpected reason: {proc.stderr.strip()}")


def main() -> int:
    try:
        with tempfile.TemporaryDirectory(prefix="genblaze-e2e-") as tmp:
            run_smoke(Path(tmp).resolve())
    except SmokeFailure as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    print(
        "OK: e2e smoke passed (pipeline -> embed -> extract -> verify --fetch -> tamper detected)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
