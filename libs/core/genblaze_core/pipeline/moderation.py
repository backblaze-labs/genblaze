"""Moderation hooks — content screening before/after generation steps."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from genblaze_core.models.asset import Asset


@dataclass
class ModerationResult:
    """Result of a moderation check.

    Attributes:
        allowed: True if the content passed moderation.
        reason: Human-readable explanation when rejected.
        flagged_categories: Category labels for flagged content (e.g. "violence", "nsfw").
    """

    allowed: bool
    reason: str | None = None
    flagged_categories: list[str] = field(default_factory=list)


class ModerationHook(ABC):
    """Abstract hook for content screening before/after generation steps.

    Implement check_prompt() and check_output() for sync execution.
    Override acheck_prompt() and acheck_output() for native async
    (defaults wrap sync methods via asyncio.to_thread).

    Example::

        class OpenAIModerationHook(ModerationHook):
            def check_prompt(self, prompt, params):
                result = openai.moderations.create(input=prompt)
                if result.results[0].flagged:
                    return ModerationResult(
                        allowed=False,
                        reason="Content flagged by OpenAI moderation",
                        flagged_categories=result.results[0].categories,
                    )
                return ModerationResult(allowed=True)

            def check_output(self, assets):
                return ModerationResult(allowed=True)
    """

    @abstractmethod
    def check_prompt(
        self,
        prompt: str | None,
        params: dict[str, Any],
    ) -> ModerationResult:
        """Screen prompt text before generation.

        Return allowed=False to skip the step (marked FAILED).
        """
        ...

    @abstractmethod
    def check_output(self, assets: list[Asset]) -> ModerationResult:
        """Screen output assets after generation.

        Return allowed=False to mark the step as FAILED.
        """
        ...

    async def acheck_prompt(
        self,
        prompt: str | None,
        params: dict[str, Any],
    ) -> ModerationResult:
        """Async prompt check. Default wraps sync via asyncio.to_thread."""
        return await asyncio.to_thread(self.check_prompt, prompt, params)

    async def acheck_output(self, assets: list[Asset]) -> ModerationResult:
        """Async output check. Default wraps sync via asyncio.to_thread."""
        return await asyncio.to_thread(self.check_output, assets)
