"""Step-level cache for pipeline results."""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from pathlib import Path

from genblaze_core.canonical.json import canonical_hash
from genblaze_core.models.step import Step

logger = logging.getLogger("genblaze.cache")


def step_cache_key(step: Step) -> str:
    """Compute deterministic cache key from step inputs.

    Key is derived from: provider, model, prompt, params, seed, modality,
    step_type, and input asset IDs (for chain mode correctness).
    """
    key_data = {
        "provider": step.provider,
        "model": step.model,
        "prompt": step.prompt,
        "params": step.params,
        "seed": step.seed,
        "modality": step.modality.value if step.modality else None,
        "step_type": step.step_type.value if step.step_type else None,
        # Use content hash or URL for cache correctness — asset_id is random per execution
        "input_ids": sorted(a.sha256 or a.url for a in step.inputs) if step.inputs else None,
    }
    return canonical_hash(key_data)


class StepCache:
    """File-based cache for completed step results.

    Stores step outputs keyed by deterministic hash of step inputs.
    Cache hits skip provider calls entirely. Thread-safe.
    """

    def __init__(self, cache_dir: str | Path) -> None:
        self._dir = Path(cache_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _path(self, key: str) -> Path:
        return self._dir / f"{key}.json"

    def get(self, step: Step) -> Step | None:
        """Return cached step result, or None if not cached (race-free)."""
        key = step_cache_key(step)
        path = self._path(key)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return Step.model_validate(data)
        except FileNotFoundError:
            return None
        except Exception as exc:
            logger.warning("Cache entry corrupt or unreadable (treating as miss): %s", exc)
            return None

    def put(self, step: Step, result: Step) -> None:
        """Cache a completed step result (atomic write, thread-safe)."""
        key = step_cache_key(step)
        path = self._path(key)
        data = result.model_dump_json().encode("utf-8")
        fd, tmp = tempfile.mkstemp(dir=self._dir, suffix=".tmp")
        fd_closed = False
        try:
            os.write(fd, data)
            os.close(fd)
            fd_closed = True
            with self._lock:
                os.replace(tmp, path)
        except BaseException:
            if not fd_closed:
                try:
                    os.close(fd)
                except OSError:
                    pass
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def clear(self) -> None:
        """Remove all cached entries (thread-safe)."""
        with self._lock:
            for f in self._dir.glob("*.json"):
                try:
                    f.unlink()
                except FileNotFoundError:
                    pass
