"""Runnable ABC — composable unit of work."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from typing import Generic, TypeVar

from genblaze_core.runnable.config import RunnableConfig

In = TypeVar("In")
Out = TypeVar("Out")


class Runnable(ABC, Generic[In, Out]):
    """Abstract base for all composable runnables.

    Subclasses must implement `invoke`. `ainvoke` has a default
    implementation that delegates to invoke in a thread.
    """

    @abstractmethod
    def invoke(self, input: In, config: RunnableConfig | None = None) -> Out:
        """Run the runnable synchronously."""
        ...

    async def ainvoke(self, input: In, config: RunnableConfig | None = None) -> Out:
        """Run the runnable asynchronously.

        Default runs invoke() in a thread to avoid blocking the event loop.
        Override in subclasses for truly async implementations.
        """
        return await asyncio.to_thread(self.invoke, input, config)
