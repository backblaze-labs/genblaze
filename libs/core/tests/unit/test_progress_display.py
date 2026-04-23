"""Tests for the terminal spinner and Pipeline auto-enable behavior."""

from __future__ import annotations

import io
import os
from unittest.mock import patch

from genblaze_core import Modality, Pipeline
from genblaze_core.progress_display import (
    Spinner,
    _fmt_elapsed,
    _truncate,
    should_auto_enable,
)
from genblaze_core.providers.progress import ProgressEvent
from genblaze_core.testing import MockVideoProvider

# --- should_auto_enable ------------------------------------------------------


def test_user_on_progress_disables_auto() -> None:
    """An explicit on_progress callback always suppresses the spinner."""
    assert should_auto_enable(on_progress=lambda _ev: None, progress=None) is False
    assert should_auto_enable(on_progress=lambda _ev: None, progress=True) is False


def test_explicit_false_disables_auto() -> None:
    assert should_auto_enable(on_progress=None, progress=False) is False


def test_non_tty_disables_auto() -> None:
    """pytest captures stderr — never a TTY — so auto should resolve False."""
    with patch("genblaze_core.progress_display.stderr_is_tty", return_value=False):
        assert should_auto_enable(on_progress=None, progress=None) is False


def test_tty_enables_auto() -> None:
    with patch("genblaze_core.progress_display.stderr_is_tty", return_value=True):
        prior = os.environ.pop("GENBLAZE_NO_PROGRESS", None)
        try:
            assert should_auto_enable(on_progress=None, progress=None) is True
        finally:
            if prior is not None:
                os.environ["GENBLAZE_NO_PROGRESS"] = prior


def test_env_var_disables_auto() -> None:
    with patch("genblaze_core.progress_display.stderr_is_tty", return_value=True):
        with patch.dict(os.environ, {"GENBLAZE_NO_PROGRESS": "1"}):
            assert should_auto_enable(on_progress=None, progress=None) is False


def test_explicit_true_overrides_tty_gate() -> None:
    with patch("genblaze_core.progress_display.stderr_is_tty", return_value=False):
        assert should_auto_enable(on_progress=None, progress=True) is True


# --- Spinner formatting -----------------------------------------------------


def test_fmt_elapsed_seconds() -> None:
    assert _fmt_elapsed(0) == "00s"
    assert _fmt_elapsed(7.8) == "07s"
    assert _fmt_elapsed(59.9) == "59s"


def test_fmt_elapsed_minutes() -> None:
    assert _fmt_elapsed(60) == "01:00"
    assert _fmt_elapsed(125) == "02:05"


def test_fmt_elapsed_hours() -> None:
    assert _fmt_elapsed(3725) == "1:02:05"


def test_truncate_short_passthrough() -> None:
    assert _truncate("hello", 80) == "hello"


def test_truncate_long_adds_ellipsis() -> None:
    out = _truncate("a" * 200, 20)
    assert len(out) == 20
    assert out.endswith("…")


def test_truncate_collapses_newlines() -> None:
    assert _truncate("line\nbreak", 80) == "line break"


# --- Spinner lifecycle on a StringIO buffer ---------------------------------


def _run_spinner_cycle(prompt: str | None) -> str:
    """Exercise one step_starting → event → step_done cycle."""
    buf = io.StringIO()
    # StringIO has no ``encoding`` attribute; spinner falls back to ASCII — good.
    s = Spinner(stream=buf)
    s.start()
    try:
        s.step_starting("gmicloud", "seedance-2-0-260128", prompt=prompt)
        s(
            ProgressEvent(
                step_id="step-1",
                provider="gmicloud",
                model="seedance-2-0-260128",
                status="processing",
                progress_pct=None,
                elapsed_sec=0.1,
            )
        )
        s.step_done(ok=True)
    finally:
        s.stop()
    return buf.getvalue()


def test_spinner_announces_with_prompt() -> None:
    out = _run_spinner_cycle("A drone shot soaring over the coast")
    assert "gmicloud:seedance-2-0-260128" in out
    assert "A drone shot soaring over the coast" in out
    # ASCII fallback for StringIO (no encoding attr).
    assert "> " in out  # header prefix
    assert "OK" in out  # final line


def test_spinner_announces_without_prompt() -> None:
    """Gracefully omits the quoted prompt when not provided."""
    out = _run_spinner_cycle(prompt=None)
    assert "gmicloud:seedance-2-0-260128" in out
    assert '"' not in out  # no empty quoted string
    assert "OK" in out


def test_spinner_failure_marks_fail() -> None:
    buf = io.StringIO()
    s = Spinner(stream=buf)
    s.start()
    try:
        s.step_starting("p", "m", prompt="x")
        s.step_done(ok=False)
    finally:
        s.stop()
    assert "FAIL" in buf.getvalue()


def test_spinner_step_done_without_start_is_noop() -> None:
    """Defensive: step_done before step_starting must not raise."""
    buf = io.StringIO()
    s = Spinner(stream=buf)
    s.start()
    try:
        s.step_done(ok=True)  # should silently no-op
    finally:
        s.stop()
    # No step line was printed.
    assert "OK" not in buf.getvalue()
    assert "FAIL" not in buf.getvalue()


# --- Pipeline integration ---------------------------------------------------


def test_pipeline_run_progress_false_disables_spinner() -> None:
    """progress=False → no spinner instantiated, no TTY writes."""
    # Force what would normally auto-enable.
    with patch("genblaze_core.progress_display.stderr_is_tty", return_value=True):
        result = (
            Pipeline("no-spinner")
            .step(MockVideoProvider(), model="mock", prompt="cat", modality=Modality.VIDEO)
            .run(progress=False)
        )
    assert result.run.steps[0].status.name == "SUCCEEDED"


def test_pipeline_run_user_on_progress_wins() -> None:
    """When the caller supplies on_progress, no spinner is layered on top."""
    events: list[ProgressEvent] = []

    with patch("genblaze_core.progress_display.stderr_is_tty", return_value=True):
        (
            Pipeline("user-callback")
            .step(MockVideoProvider(), model="mock", prompt="cat", modality=Modality.VIDEO)
            .run(on_progress=events.append)
        )

    # The user callback saw at least the terminal event(s) — spinner would
    # have swallowed these into state-only updates.
    assert any(ev.status == "succeeded" for ev in events)


def test_pipeline_run_auto_disabled_on_non_tty() -> None:
    """In a captured-pytest environment, stderr is not a TTY → no spinner."""
    # No patch: actual isatty() in pytest is False. Just assert the run succeeds
    # cleanly with no progress-related noise leaking.
    result = (
        Pipeline("non-tty")
        .step(MockVideoProvider(), model="mock", prompt="cat", modality=Modality.VIDEO)
        .run()
    )
    assert result.run.steps[0].status.name == "SUCCEEDED"
