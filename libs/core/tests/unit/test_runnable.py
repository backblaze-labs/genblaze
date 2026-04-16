"""Tests for Runnable ABC."""

import asyncio

from genblaze_core.runnable.base import Runnable
from genblaze_core.runnable.config import RunnableConfig


class AddOne(Runnable[int, int]):
    def invoke(self, input: int, config: RunnableConfig | None = None) -> int:
        return input + 1


def test_invoke():
    r = AddOne()
    assert r.invoke(1) == 2


def test_ainvoke():
    r = AddOne()
    assert asyncio.run(r.ainvoke(5)) == 6
