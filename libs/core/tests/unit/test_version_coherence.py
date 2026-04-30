"""Tests for ``__version__`` coherence across published packages.

The bug being closed (storage tranche bug #9): pre-fix,
``genblaze_core.__version__`` was a hardcoded string that drifted
out of sync with ``importlib.metadata.version("genblaze-core")``
across releases. The user-agent header
(``b2ai-genblaze/{__version__}``) silently shipped the wrong version.

Post-fix, ``__version__`` reads from ``importlib.metadata`` so the
two are always equal. Same pattern for the umbrella ``genblaze``
package.
"""

from __future__ import annotations

import importlib.metadata


class TestVersionCoherence:
    def test_genblaze_core_version_matches_metadata(self):
        """``genblaze_core.__version__`` must equal
        ``importlib.metadata.version('genblaze-core')`` exactly."""
        import genblaze_core

        metadata_version = importlib.metadata.version("genblaze-core")
        assert genblaze_core.__version__ == metadata_version

    def test_genblaze_umbrella_version_matches_metadata(self):
        """Same coherence guarantee for the umbrella package."""
        try:
            import genblaze
        except ImportError:
            # Umbrella isn't always installed in dev envs that work
            # purely against ``genblaze-core``. Skip rather than fail.
            import pytest

            pytest.skip("umbrella `genblaze` package not installed")

        metadata_version = importlib.metadata.version("genblaze")
        assert genblaze.__version__ == metadata_version

    def test_user_agent_uses_same_version_as_module(self):
        """The S3 backend's user-agent header must read from the same
        source as ``genblaze_core.__version__`` so logs / B2 attribution
        always match the installed wheel."""
        try:
            from genblaze_s3.backend import _USER_AGENT
        except ImportError:
            import pytest

            pytest.skip("genblaze-s3 connector not installed")

        import genblaze_core

        # _USER_AGENT shape: "b2ai-genblaze/{version}"
        assert _USER_AGENT.endswith(f"/{genblaze_core.__version__}")

    def test_version_is_non_empty_string(self):
        """Sanity: the metadata-derived version is a real string, not
        accidentally None / empty / a numeric type."""
        import genblaze_core

        assert isinstance(genblaze_core.__version__, str)
        assert len(genblaze_core.__version__) > 0


class TestConnectorVersionCoherence:
    """Each genblaze-* connector's ``__version__`` must read from
    ``importlib.metadata`` rather than a hardcoded string. Plan 5
    Phase 1B closed the version-drift class of bug across all 13
    connector packages."""

    @staticmethod
    def _check(module_name: str, dist_name: str) -> None:
        try:
            mod = __import__(module_name)
        except ImportError:
            import pytest

            pytest.skip(f"{module_name} not installed")
        assert hasattr(mod, "__version__"), (
            f"{module_name} does not expose __version__ — Phase 1B regression"
        )
        assert mod.__version__ == importlib.metadata.version(dist_name), (
            f"{module_name}.__version__ ({mod.__version__}) drifted from "
            f"importlib.metadata.version({dist_name!r})"
        )

    def test_genblaze_s3(self):
        self._check("genblaze_s3", "genblaze-s3")

    def test_genblaze_openai(self):
        self._check("genblaze_openai", "genblaze-openai")

    def test_genblaze_google(self):
        self._check("genblaze_google", "genblaze-google")

    def test_genblaze_replicate(self):
        self._check("genblaze_replicate", "genblaze-replicate")

    def test_genblaze_runway(self):
        self._check("genblaze_runway", "genblaze-runway")

    def test_genblaze_luma(self):
        self._check("genblaze_luma", "genblaze-luma")

    def test_genblaze_decart(self):
        self._check("genblaze_decart", "genblaze-decart")

    def test_genblaze_elevenlabs(self):
        self._check("genblaze_elevenlabs", "genblaze-elevenlabs")

    def test_genblaze_lmnt(self):
        self._check("genblaze_lmnt", "genblaze-lmnt")

    def test_genblaze_stability_audio(self):
        self._check("genblaze_stability_audio", "genblaze-stability-audio")

    def test_genblaze_nvidia(self):
        self._check("genblaze_nvidia", "genblaze-nvidia")

    def test_genblaze_gmicloud(self):
        self._check("genblaze_gmicloud", "genblaze-gmicloud")

    def test_genblaze_langsmith(self):
        self._check("genblaze_langsmith", "genblaze-langsmith")
