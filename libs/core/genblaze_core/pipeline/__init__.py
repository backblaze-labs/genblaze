"""High-level Pipeline API.

Uses PEP 562 module-level ``__getattr__`` to lazy-load the heavy
``pipeline.py`` module only on first attribute access. This keeps
``import genblaze_core.pipeline`` cheap and — more importantly — lets
downstream modules (notably ``genblaze_core.observability.events``)
import from ``genblaze_core.pipeline.result`` without triggering a
circular import through ``pipeline.py`` → ``observability.events``.

Public surface is unchanged: ``from genblaze_core.pipeline import
Pipeline, StepCache, PipelineResult, StepCompleteEvent`` keeps working
exactly as before.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

_LAZY_IMPORTS: dict[str, tuple[str, str]] = {
    "Pipeline": ("genblaze_core.pipeline.pipeline", "Pipeline"),
    "PipelineResult": ("genblaze_core.pipeline.result", "PipelineResult"),
    "StepCache": ("genblaze_core.pipeline.cache", "StepCache"),
    "StepCompleteEvent": ("genblaze_core.pipeline.result", "StepCompleteEvent"),
}

__all__ = sorted(_LAZY_IMPORTS.keys())


def __getattr__(name: str) -> Any:
    if name in _LAZY_IMPORTS:
        import importlib

        module_path, attr = _LAZY_IMPORTS[name]
        mod = importlib.import_module(module_path)
        val = getattr(mod, attr)
        globals()[name] = val  # cache: subsequent accesses skip __getattr__
        return val
    raise AttributeError(f"module 'genblaze_core.pipeline' has no attribute {name!r}")


def __dir__() -> list[str]:
    return __all__


if TYPE_CHECKING:
    # Type checkers (mypy, pyright) don't execute __getattr__, so declare
    # the re-exports statically for IDE autocomplete and `from ... import`
    # resolution. Redundant ``as`` aliases mark these as intentional
    # re-exports so ruff's F401 treats them correctly. Erased at runtime.
    from genblaze_core.pipeline.cache import StepCache as StepCache
    from genblaze_core.pipeline.pipeline import Pipeline as Pipeline
    from genblaze_core.pipeline.result import PipelineResult as PipelineResult
    from genblaze_core.pipeline.result import StepCompleteEvent as StepCompleteEvent
