"""Shared import smoke check for release verification."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Iterable, Mapping, Sequence

DEFAULT_PACKAGE_IMPORTS = (
    ("genblaze-core", "genblaze_core"),
    ("genblaze-s3", "genblaze_s3"),
)

# The umbrella module and storage submodule are explicit probes in addition
# to the imports that map directly to the umbrella package dependencies.
CORE_IMPORTS = (
    "genblaze",
    *(module for _package, module in DEFAULT_PACKAGE_IMPORTS),
    "genblaze_core.storage",
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
DEFAULT_IMPORT_TIMEOUT_SECONDS = 20.0
TIMEOUT_ENV = "GENBLAZE_IMPORT_SMOKE_TIMEOUT"

ENV_ALLOWLIST = frozenset(
    {
        "COMSPEC",
        "CURL_CA_BUNDLE",
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
        "WINDIR",
    }
)
SANDBOX_PATH_ENV = ("HOME", "USERPROFILE", "TMPDIR", "TMP", "TEMP")
SENSITIVE_ENV_NAME_RE = re.compile(
    r"(?:"
    r"API|AUTH|AZURE|B2_|BACKBLAZE|CREDENTIAL|GCP|GITHUB|GOOGLE|KEY|NPM|OPENAI|"
    r"PASSWORD|PYPI|SECRET|TOKEN|TWINE|AWS|ANTHROPIC|ASSEMBLYAI|ELEVEN|GMI|"
    r"HUME|LANGSMITH|LMNT|LUMA|NVIDIA|REPLICATE|RUNWAY|STABILITY"
    r")",
    re.IGNORECASE,
)
CHILD_IMPORT = """
import importlib
import json
import sys
import traceback

module_name = sys.argv[1]
extra_paths = json.loads(sys.argv[2])
for path in reversed(extra_paths):
    sys.path.insert(0, path)

try:
    importlib.import_module(module_name)
except Exception as exc:
    traceback.print_exception(type(exc), exc, exc.__traceback__)
    raise SystemExit(1)
"""


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


def _import_timeout_seconds(env: Mapping[str, str]) -> float:
    raw_timeout = env.get(TIMEOUT_ENV)
    if raw_timeout is None:
        return DEFAULT_IMPORT_TIMEOUT_SECONDS
    try:
        timeout = float(raw_timeout)
    except ValueError as exc:
        raise ValueError(f"{TIMEOUT_ENV} must be a positive number of seconds") from exc
    if timeout <= 0:
        raise ValueError(f"{TIMEOUT_ENV} must be a positive number of seconds")
    return timeout


def _sandbox_environment(original_env: Mapping[str, str], sandbox_home: str) -> dict[str, str]:
    safe_env = {
        name: value
        for name, value in original_env.items()
        if name.upper() in ENV_ALLOWLIST and not SENSITIVE_ENV_NAME_RE.search(name)
    }
    safe_env.update({name: sandbox_home for name in SANDBOX_PATH_ENV})
    safe_env["PYTHONNOUSERSITE"] = "1"
    return safe_env


def _print_stream(label: str, content: str) -> None:
    if not content:
        return
    print(f"  {label}:")
    for line in content.splitlines():
        print(f"    {line}")


def _import_in_sandbox(
    module_name: str,
    *,
    env: Mapping[str, str],
    sandbox_home: str,
    extra_paths: Sequence[str],
    sensitive_values: Iterable[str],
    timeout_seconds: float,
) -> str | None:
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                "-c",
                CHILD_IMPORT,
                module_name,
                json.dumps(list(extra_paths)),
            ],
            cwd=sandbox_home,
            env=dict(env),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = _redact(exc.stdout or "", sensitive_values)
        stderr = _redact(exc.stderr or "", sensitive_values)
        message = f"import subprocess timed out after {timeout_seconds:g}s"
        print(f"  FAIL: {module_name}: {message}")
        _print_stream("stdout", stdout)
        _print_stream("stderr", stderr)
        return message

    stdout = _redact(result.stdout, sensitive_values)
    stderr = _redact(result.stderr, sensitive_values)
    if result.returncode == 0:
        print(f"  ok: {module_name}")
        return None

    message = f"import subprocess exited with {result.returncode}"
    print(f"  FAIL: {module_name}: {message}")
    _print_stream("stdout", stdout)
    _print_stream("stderr", stderr)
    return message


def smoke_import(
    modules: Iterable[str] = SMOKE_IMPORTS,
    *,
    extra_paths: Iterable[str] = (),
) -> list[tuple[str, str]]:
    """Import release modules in isolated subprocesses with a scrubbed env."""
    original_env = dict(os.environ)
    sensitive_values = _sensitive_env_values(original_env)
    timeout_seconds = _import_timeout_seconds(original_env)
    failures: list[tuple[str, str]] = []

    extra_path_list = tuple(os.fspath(path) for path in extra_paths)
    with tempfile.TemporaryDirectory(prefix="genblaze-import-smoke-") as sandbox_home:
        safe_env = _sandbox_environment(original_env, sandbox_home)
        for module_name in modules:
            message = _import_in_sandbox(
                module_name,
                env=safe_env,
                sandbox_home=sandbox_home,
                extra_paths=extra_path_list,
                sensitive_values=sensitive_values,
                timeout_seconds=timeout_seconds,
            )
            if message is not None:
                failures.append((module_name, message))

    return failures


def main() -> int:
    failures = smoke_import()
    if failures:
        print(f"{len(failures)} import failure(s)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
