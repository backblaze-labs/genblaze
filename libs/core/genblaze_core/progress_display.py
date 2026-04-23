"""Terminal spinner for long-running provider polls — stdlib-only.

Auto-enabled by ``Pipeline.run()`` / ``Pipeline.arun()`` when stderr is a TTY
and no ``on_progress`` callback is supplied. Opt out with ``progress=False``.

Design:
- Zero external dependencies (stdlib ``threading`` + ANSI escapes).
- One render thread animates at ~8 Hz; poll cadence is untouched.
- Main thread owns step-boundary output (header + final line); the render
  thread owns the animated status line. All stream writes are serialized
  under ``self._lock`` to avoid interleaving.
- Graceful UTF-8 fallback: braille frames and unicode marks degrade to ASCII
  when the stream encoding can't represent them.
"""

from __future__ import annotations

import os
import shutil
import sys
import threading
import time
from typing import IO, Any

_BRAILLE = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
_ASCII = "|/-\\"

_HIDE_CURSOR = "\x1b[?25l"
_SHOW_CURSOR = "\x1b[?25h"
_CLEAR_LINE = "\x1b[2K\r"

_FRAME_INTERVAL = 0.125  # 8 Hz — smooth without being a syscall hog.
_PROMPT_SNIPPET_MAX = 80


def _supports_utf8(stream: IO[str]) -> bool:
    enc = (getattr(stream, "encoding", "") or "").lower()
    return "utf-8" in enc or "utf8" in enc


def _fmt_elapsed(seconds: float) -> str:
    s = max(0, int(seconds))
    if s < 60:
        return f"{s:02d}s"
    m, r = divmod(s, 60)
    if m < 60:
        return f"{m:02d}:{r:02d}"
    h, m = divmod(m, 60)
    return f"{h:d}:{m:02d}:{r:02d}"


def _truncate(text: str, limit: int) -> str:
    text = text.replace("\n", " ").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def stderr_is_tty() -> bool:
    """True when stderr is an interactive terminal."""
    stream = sys.stderr
    try:
        return bool(getattr(stream, "isatty", lambda: False)())
    except Exception:
        return False


def should_auto_enable(on_progress: Any, progress: bool | None) -> bool:
    """Resolve the three-way ``progress`` kwarg into a boolean."""
    if on_progress is not None:
        # Explicit user callback wins — never layer a spinner on top.
        return False
    if progress is False:
        return False
    if progress is True:
        return True
    # progress is None → auto: TTY-gated.
    if os.environ.get("GENBLAZE_NO_PROGRESS"):
        return False
    return stderr_is_tty()


class Spinner:
    """Stdlib spinner; implements ``__call__`` to plug into ``on_progress``.

    Typical usage (managed by ``Pipeline.run()``):

        s = Spinner()
        s.start()
        try:
            s.step_starting("gmicloud", "seedance-2-0-260128", "A drone shot…")
            # … provider poll loop fires s(event) repeatedly …
            s.step_done(ok=True)
        finally:
            s.stop()

    The render thread is daemon + lock-gated, so a missed ``stop()`` (e.g.
    SIGKILL) won't hang interpreter shutdown.
    """

    def __init__(self, stream: IO[str] | None = None) -> None:
        self._stream = stream if stream is not None else sys.stderr
        self._utf8 = _supports_utf8(self._stream)
        self._frames = _BRAILLE if self._utf8 else _ASCII
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._cursor_hidden = False
        # Per-step animated state:
        self._active = False
        self._start_time = 0.0
        self._label = ""
        self._status = ""
        self._progress_pct: float | None = None
        self._message: str | None = None

    # --- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop_event.clear()
        with self._lock:
            try:
                self._stream.write(_HIDE_CURSOR)
                self._stream.flush()
                self._cursor_hidden = True
            except Exception:  # noqa: S110 — cosmetic ANSI on a non-TTY stream is best-effort.
                pass
        self._thread = threading.Thread(
            target=self._render_loop, name="genblaze-spinner", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        if self._thread is None:
            return
        self._stop_event.set()
        self._thread.join(timeout=1.0)
        self._thread = None
        with self._lock:
            if self._active:
                # Run interrupted mid-step; clear the animated line.
                self._write(_CLEAR_LINE)
                self._active = False
            if self._cursor_hidden:
                try:
                    self._stream.write(_SHOW_CURSOR)
                    self._stream.flush()
                except Exception:  # noqa: S110 — stream may be gone on shutdown; nothing to log to.
                    pass
                self._cursor_hidden = False

    def __enter__(self) -> Spinner:
        self.start()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.stop()

    # --- step boundaries ----------------------------------------------------

    def step_starting(
        self,
        provider: str,
        model: str,
        prompt: str | None = None,
        *,
        step_index: int | None = None,
        total: int | None = None,
    ) -> None:
        """Print the static header for a new step and begin animating."""
        label = f"{provider}:{model}".strip(":") or "generating"
        mark = "▶" if self._utf8 else ">"
        counter = ""
        if step_index is not None and total is not None and total > 1:
            counter = f" [{step_index + 1}/{total}]"
        line = f"{mark}{counter} {label}"
        if prompt:
            line += f' · "{_truncate(prompt, _PROMPT_SNIPPET_MAX)}"'
        with self._lock:
            self._write(_CLEAR_LINE + line + "\n")
            self._label = label
            self._status = "submitted"
            self._progress_pct = None
            self._message = None
            self._start_time = time.monotonic()
            self._active = True

    def step_done(self, ok: bool) -> None:
        """Finalize the current step: clear spinner, print a terminal line."""
        with self._lock:
            if not self._active:
                return
            elapsed = _fmt_elapsed(time.monotonic() - self._start_time)
            if self._utf8:
                mark = "✓" if ok else "✗"
            else:
                mark = "OK" if ok else "FAIL"
            line = f"  {mark} {self._label} · {elapsed}"
            self._write(_CLEAR_LINE + line + "\n")
            self._active = False

    # --- ProgressEvent callback --------------------------------------------

    def __call__(self, event: Any) -> None:
        """Update state from a ``ProgressEvent``. Render thread reads it."""
        with self._lock:
            if not self._active:
                # Event fired before step_starting; ignore (defensive).
                return
            self._status = getattr(event, "status", self._status) or self._status
            self._progress_pct = getattr(event, "progress_pct", None)
            self._message = getattr(event, "message", None)

    # --- render thread ------------------------------------------------------

    def _render_loop(self) -> None:
        i = 0
        while not self._stop_event.wait(_FRAME_INTERVAL):
            with self._lock:
                if not self._active:
                    continue
                frame = self._frames[i % len(self._frames)]
                elapsed = _fmt_elapsed(time.monotonic() - self._start_time)
                parts = [f"  {frame} {self._label}", self._status, elapsed]
                if self._progress_pct is not None:
                    parts.append(f"{int(self._progress_pct * 100)}%")
                if self._message:
                    parts.append(_truncate(self._message, 40))
                line = " · ".join(parts)
                line = self._fit_width(line)
                self._write(_CLEAR_LINE + line)
            i += 1

    # --- helpers ------------------------------------------------------------

    def _write(self, text: str) -> None:
        """Caller must hold ``self._lock``."""
        try:
            self._stream.write(text)
            self._stream.flush()
        except Exception:  # noqa: S110 — closed pipe / killed terminal; silent drop is correct.
            pass

    def _fit_width(self, line: str) -> str:
        try:
            width = shutil.get_terminal_size((80, 20)).columns
        except Exception:
            width = 80
        # Leave one cell so the cursor position doesn't wrap.
        limit = max(20, width - 1)
        if len(line) <= limit:
            return line
        return line[: limit - 1] + "…"
