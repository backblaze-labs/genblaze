"""Shared import smoke check for release verification."""

from __future__ import annotations

import importlib
import os
import re
import sys
from collections.abc import Iterable, Mapping

# The umbrella installs core + s3 by default; keep the storage submodule here
# because it has a hard import-time urllib3 dependency hidden by core's lazy
# top-level exports.
CORE_IMPORTS = (
    "genblaze",
    "genblaze_core",
    "genblaze_core.storage",
    "genblaze_s3",
)

# Keep this list aligned with libs/meta/pyproject.toml's genblaze[all] extra.
# tools/tests/test_release_import_smoke.py fails when a new all-extra package
# is added without a matching import smoke target.
ALL_EXTRA_PACKAGE_IMPORTS = (
    ("genblaze-gmicloud", "genblaze_gmicloud"),
    ("genblaze-openai", "genblaze_openai"),
    ("genblaze-google", "genblaze_google"),
    ("genblaze-replicate", "genblaze_replicate"),
    ("genblaze-runway", "genblaze_runway"),
    ("genblaze-luma", "genblaze_luma"),
    ("genblaze-decart", "genblaze_decart"),
    ("genblaze-elevenlabs", "genblaze_elevenlabs"),
    ("genblaze-stability-audio", "genblaze_stability_audio"),
    ("genblaze-lmnt", "genblaze_lmnt"),
    ("genblaze-hume", "genblaze_hume"),
    ("genblaze-assemblyai", "genblaze_assemblyai"),
    ("genblaze-langsmith", "genblaze_langsmith"),
    ("genblaze-nvidia", "genblaze_nvidia"),
)

CONNECTOR_IMPORTS = tuple(module for _package, module in ALL_EXTRA_PACKAGE_IMPORTS)
SMOKE_IMPORTS = CORE_IMPORTS + CONNECTOR_IMPORTS
REDACTION = "[redacted]"

ENV_ALLOWLIST = frozenset(
    {
        "COMSPEC",
        "CURL_CA_BUNDLE",
        "HOME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "PATH",
        "PATHEXT",
        "PYTHONIOENCODING",
        "PYTHONUTF8",
        "PYTHONWARNINGS",
        "REQUESTS_CA_BUNDLE",
        "SSL_CERT_FILE",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "USERPROFILE",
        "WINDIR",
    }
)
SENSITIVE_ENV_NAME_RE = re.compile(
    r"(?:"
    r"API|AUTH|AZURE|B2_|BACKBLAZE|CREDENTIAL|GCP|GITHUB|GOOGLE|KEY|NPM|OPENAI|"
    r"PASSWORD|PYPI|SECRET|TOKEN|TWINE|AWS|ANTHROPIC|ASSEMBLYAI|ELEVEN|GMI|"
    r"HUME|LANGSMITH|LMNT|LUMA|NVIDIA|REPLICATE|RUNWAY|STABILITY"
    r")",
    re.IGNORECASE,
)


def _sensitive_env_values(env: Mapping[str, str]) -> tuple[str, ...]:
    values = {
        value
        for name, value in env.items()
        if len(value) >= 4 and SENSITIVE_ENV_NAME_RE.search(name)
    }
    return tuple(sorted(values, key=len, reverse=True))


def _redact(message: str, sensitive_values: Iterable[str]) -> str:
    redacted = message
    for value in sensitive_values:
        redacted = redacted.replace(value, REDACTION)
    return redacted


def _sanitize_environment(original_env: Mapping[str, str]) -> None:
    safe_env = {
        name: value for name, value in original_env.items() if name.upper() in ENV_ALLOWLIST
    }
    os.environ.clear()
    os.environ.update(safe_env)


def smoke_import(modules: Iterable[str] = SMOKE_IMPORTS) -> list[tuple[str, str]]:
    """Import release modules after removing ambient secrets from this process."""
    original_env = dict(os.environ)
    sensitive_values = _sensitive_env_values(original_env)
    failures: list[tuple[str, str]] = []

    _sanitize_environment(original_env)
    for module_name in modules:
        try:
            importlib.import_module(module_name)
        except Exception as exc:
            message = _redact(f"{type(exc).__name__}: {exc}", sensitive_values)
            failures.append((module_name, message))
            print(f"  FAIL: {module_name}: {message}")
        else:
            print(f"  ok: {module_name}")

    return failures


def main() -> int:
    failures = smoke_import()
    if failures:
        print(f"{len(failures)} import failure(s)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
