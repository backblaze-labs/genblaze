"""Evaluator — pluggable quality-gate for pipeline outputs.

An ``Evaluator`` inspects a :class:`PipelineResult` and returns an
:class:`EvaluationResult` describing whether the output meets quality
requirements. Evaluators can be simple threshold checks, LLM judges,
vision-model classifiers, or composed chains.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

    from genblaze_core.pipeline.result import PipelineResult


@dataclass
class EvaluationResult:
    """Outcome of evaluating a pipeline result.

    Attributes:
        passed: Whether the output meets quality requirements.
        score: Optional numeric score (typically 0.0–1.0). None if not applicable.
        feedback: Human-readable explanation to feed back into a refinement step.
        metadata: Arbitrary structured data (e.g. per-dimension scores, detected issues).
    """

    passed: bool
    score: float | None = None
    feedback: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class Evaluator(ABC):
    """Abstract evaluator. Subclasses implement at least ``evaluate``."""

    @abstractmethod
    def evaluate(self, result: PipelineResult) -> EvaluationResult:
        """Judge a completed pipeline result. Must return an EvaluationResult."""

    async def aevaluate(self, result: PipelineResult) -> EvaluationResult:
        """Async variant. Default runs ``evaluate`` in a thread."""
        return await asyncio.to_thread(self.evaluate, result)


class CallableEvaluator(Evaluator):
    """Wraps a plain callable as an :class:`Evaluator`.

    The callable receives the :class:`PipelineResult` and returns either
    an :class:`EvaluationResult` or a bool (passed/not).
    """

    def __init__(
        self,
        fn: Callable[[PipelineResult], EvaluationResult | bool],
    ) -> None:
        self._fn = fn

    def evaluate(self, result: PipelineResult) -> EvaluationResult:
        out = self._fn(result)
        if isinstance(out, EvaluationResult):
            return out
        return EvaluationResult(passed=bool(out))


class ThresholdEvaluator(Evaluator):
    """Pass/fail based on a numeric score crossing a threshold.

    ``score_fn`` receives the :class:`PipelineResult` and returns a float.
    Useful for wrapping a vision or text quality model that emits a score.
    """

    def __init__(
        self,
        score_fn: Callable[[PipelineResult], float],
        threshold: float,
        *,
        higher_is_better: bool = True,
        feedback_fn: Callable[[PipelineResult, float], str] | None = None,
    ) -> None:
        self._score_fn = score_fn
        self._threshold = threshold
        self._higher_is_better = higher_is_better
        self._feedback_fn = feedback_fn

    def evaluate(self, result: PipelineResult) -> EvaluationResult:
        score = float(self._score_fn(result))
        passed = score >= self._threshold if self._higher_is_better else score <= self._threshold
        feedback = self._feedback_fn(result, score) if self._feedback_fn else None
        return EvaluationResult(
            passed=passed,
            score=score,
            feedback=feedback,
            metadata={"threshold": self._threshold, "higher_is_better": self._higher_is_better},
        )
