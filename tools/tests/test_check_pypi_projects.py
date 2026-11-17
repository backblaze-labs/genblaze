"""Tests for tools/check_pypi_projects.py. PyPI is never contacted: urlopen is mocked."""

from __future__ import annotations

import io
import sys
import urllib.error
from pathlib import Path
from unittest.mock import patch

import pytest

_TOOLS_DIR = Path(__file__).resolve().parent.parent
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

import check_pypi_projects as cpp  # noqa: E402
import prepare_release as pr  # noqa: E402

_REPO_ROOT = _TOOLS_DIR.parent
_PACKAGES = pr.discover_packages(_REPO_ROOT)
# Real distribution names (the 0.8.0 first-publish failures), read from source.
FAL = _PACKAGES["fal"].name
ATLAS = _PACKAGES["atlascloud"].name


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://pypi.org/x", code, "err", None, io.BytesIO())


class _Resp:
    """Minimal context-manager stand-in for a 200 urlopen response."""

    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _urlopen_by_name(outcomes: dict[str, object]):
    """Build a urlopen fake keyed on the project name in the URL.

    Each outcome is either ``200`` or an exception instance to raise.
    Unlisted names default to 200 (project exists).
    """

    def fake(url, timeout=None):
        name = url.removeprefix(cpp.PYPI_JSON_URL.split("{")[0]).split("/")[0]
        outcome = outcomes.get(name, 200)
        if isinstance(outcome, BaseException):
            raise outcome
        return _Resp()

    return fake


# ---------------------------------------------------------------------------
# project_status(): one HTTP outcome -> one status
# ---------------------------------------------------------------------------


def test_status_exists_on_200():
    with patch.object(cpp.urllib.request, "urlopen", _urlopen_by_name({})):
        assert cpp.project_status("genblaze-core") == cpp.EXISTS


def test_status_new_on_404():
    fake = _urlopen_by_name({FAL: _http_error(404)})
    with patch.object(cpp.urllib.request, "urlopen", fake):
        assert cpp.project_status(FAL) == cpp.NEW


@pytest.mark.parametrize(
    "exc",
    [
        _http_error(500),
        _http_error(503),
        _http_error(429),
        urllib.error.URLError("nodename nor servname provided"),
        TimeoutError("timed out"),
        ConnectionResetError("reset"),
    ],
)
def test_status_unknown_on_network_or_server_failure(exc):
    # Only an explicit 404 means "no project"; everything else is unverified.
    fake = _urlopen_by_name({FAL: exc})
    with patch.object(cpp.urllib.request, "urlopen", fake):
        assert cpp.project_status(FAL) == cpp.UNKNOWN


def test_status_url_targets_project_json_endpoint():
    seen: list[str] = []

    def fake(url, timeout=None):
        seen.append(url)
        assert timeout, "request must carry a timeout"
        return _Resp()

    with patch.object(cpp.urllib.request, "urlopen", fake):
        cpp.project_status("genblaze-core")
    assert seen == ["https://pypi.org/pypi/genblaze-core/json"]


# ---------------------------------------------------------------------------
# publishable_names(): reuses prepare_release discovery, Python only
# ---------------------------------------------------------------------------


def test_publishable_names_match_discovered_pyproject_packages():
    expected = sorted(p.name for p in _PACKAGES.values() if p.kind == "pyproject")
    names = cpp.publishable_names(_REPO_ROOT)
    assert names == expected
    # npm package is out of scope (npm Trusted Publishing is separate, §c).
    assert _PACKAGES["spec"].name not in names
    assert {"genblaze-core", "genblaze-cli", "genblaze"} <= set(names)


# ---------------------------------------------------------------------------
# main(): warning-only gate, prominent output
# ---------------------------------------------------------------------------


def _run_main(capsys, outcomes: dict[str, object]) -> tuple[int, str]:
    with patch.object(cpp.urllib.request, "urlopen", _urlopen_by_name(outcomes)):
        code = cpp.main([])
    return code, capsys.readouterr().out


def test_main_all_exist_prints_no_warning(capsys):
    code, out = _run_main(capsys, {})
    assert code == 0
    assert "WARNING" not in out
    assert "genblaze-core" in out


def test_main_new_project_warns_with_publisher_fields_and_exits_zero(capsys):
    code, out = _run_main(capsys, {FAL: _http_error(404)})
    assert code == 0  # new connectors are legitimate; never block the gate
    assert "WARNING" in out
    banner = out[out.index("WARNING") :]
    assert FAL in banner
    assert "genblaze-core" not in banner  # only new projects are listed
    for field in ("backblaze-labs", "genblaze", "release.yml", "pypi"):
        assert field in banner
    assert "RELEASING.md" in banner and "§a" in banner
    assert "pending" in banner.lower()


def test_main_network_failure_is_not_reported_as_new(capsys):
    code, out = _run_main(capsys, {FAL: urllib.error.URLError("offline")})
    assert code == 0
    assert "could not" in out.lower()
    # Unverified packages must not land in the add-a-publisher list.
    assert "pending Trusted Publisher" not in out


def test_main_reports_new_and_unknown_separately(capsys):
    code, out = _run_main(
        capsys,
        {FAL: _http_error(404), ATLAS: _http_error(502)},
    )
    assert code == 0
    # Banner prints last so it sits right above make's final "gates passed".
    unverified = out[out.lower().index("could not") : out.index("WARNING")]
    banner = out[out.index("WARNING") :]
    assert ATLAS in unverified and FAL not in unverified
    assert FAL in banner and ATLAS not in banner
    total = len(cpp.publishable_names(_REPO_ROOT))
    assert f"Summary: {total - 2} on PyPI, 1 new, 1 unverified" in out
