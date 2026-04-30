"""Tests for ``genblaze_s3._user_agent.build_user_agent``.

Plan 5 Phase 1C — replaces the hardcoded ``_USER_AGENT`` constant
that drifted out of sync with the actual installed wheel version.
The helper now reads from ``genblaze_core._version`` (which itself
reads from ``importlib.metadata``), so the user-agent always
reflects the installed version.
"""

from __future__ import annotations

from genblaze_s3._user_agent import build_user_agent


class TestBuildUserAgent:
    def test_default_base_carries_version(self):
        """Default base is ``b2ai-genblaze/<version>`` — version comes
        from ``genblaze_core._version`` so it tracks the installed wheel."""
        import genblaze_core

        ua = build_user_agent()
        assert ua == f"b2ai-genblaze/{genblaze_core.__version__}"

    def test_extra_appended_after_base(self):
        ua = build_user_agent(extra="my-app/1.2.3")
        assert ua.endswith(" my-app/1.2.3")
        assert ua.startswith("b2ai-genblaze/")

    def test_custom_base_replaces_default(self):
        ua = build_user_agent(base="my-fork/0.1")
        assert ua == "my-fork/0.1"

    def test_base_and_extra_compose(self):
        ua = build_user_agent(base="my-fork/0.1", extra="ext/2.0")
        assert ua == "my-fork/0.1 ext/2.0"

    def test_no_trailing_whitespace_when_extra_empty(self):
        """``extra=""`` should not produce a trailing space."""
        ua = build_user_agent(extra="")
        assert ua == build_user_agent()
        assert not ua.endswith(" ")
