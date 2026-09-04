"""Tests for the BaseProvider construction guard (issue #261).

A ``@dataclass``-decorated provider subclass (or one with a hand-written
``__init__`` that skips ``super().__init__()``) must fail loudly at
construction, not silently succeed and blow up later inside ``invoke()``.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from genblaze_core.exceptions import ProviderError
from genblaze_core.models.step import Step
from genblaze_core.providers.base import SyncProvider


class _GoodSync(SyncProvider):
    name = "good-sync"

    def generate(self, step: Step, config=None) -> Step:
        return step


class _GoodMid(SyncProvider):
    """Intermediate subclass with no __init__ of its own."""

    name = "good-mid"

    def generate(self, step: Step, config=None) -> Step:
        return step


class _GoodLeaf(_GoodMid):
    """Multi-level subclass, still no __init__ override."""

    name = "good-leaf"


def test_correct_provider_constructs_and_invokes():
    provider = _GoodSync()
    step = Step(provider="good-sync", model="m", prompt="hi")
    result = provider.invoke(step)
    assert result.status.value == "succeeded"


def test_multi_level_subclass_constructs():
    provider = _GoodLeaf()
    assert provider._base_init_token is not None


def test_dataclass_provider_raises_typeerror_at_construction():
    @dataclass
    class DataclassProvider(SyncProvider):
        api_key: str = "k"

        @property
        def name(self):
            return "dataclass-prov"

        def generate(self, step: Step, config=None) -> Step:
            return step

    with pytest.raises(TypeError) as exc_info:
        DataclassProvider()

    message = str(exc_info.value)
    assert "@dataclass" in message
    assert "super().__init__()" in message
    # The old symptom from the issue must not resurface as an
    # AttributeError bleeding out of construction.
    assert "_retry_policy_override" not in message


def test_hand_written_init_without_super_raises_typeerror():
    class NoSuperInit(SyncProvider):
        name = "no-super"

        def __init__(self):
            pass

        def generate(self, step: Step, config=None) -> Step:
            return step

    with pytest.raises(TypeError) as exc_info:
        NoSuperInit()

    assert "super().__init__()" in str(exc_info.value)


def test_dataclass_provider_never_reaches_the_old_attributeerror():
    """Before the fix, MyProv() constructed fine and invoke() raised
    AttributeError twice (first _poll_cache_max_age, then masked by
    _retry_policy_override). Now construction itself fails, so invoke()
    is never reached with a broken instance."""

    @dataclass
    class MyProv(SyncProvider):
        api_key: str = "k"

        @property
        def name(self):
            return "myprov"

        def generate(self, step: Step, config=None) -> Step:
            return step

    with pytest.raises(TypeError):
        MyProv()


class _BrokenBypassMeta(SyncProvider):
    """A provider whose instance never ran __init__ at all, constructed via
    object.__new__ to bypass the metaclass guard entirely (simulating any
    other route to a half-initialized instance, e.g. pickle/copy internals)."""

    name = "broken-bypass"

    def generate(self, step: Step, config=None) -> Step:
        return step


def test_invoke_reraises_original_error_when_bypassing_the_guard():
    """If an instance somehow bypasses the metaclass (object.__new__), invoke()'s
    error handler must not mask the real AttributeError with a second one from
    touching self.retry_policy."""
    provider = object.__new__(_BrokenBypassMeta)
    step = Step(provider="broken-bypass", model="m", prompt="hi")

    with pytest.raises(AttributeError) as exc_info:
        provider.invoke(step)

    # Must be the *original* failure (from _cleanup_poll_cache reading
    # _poll_cache_max_age), not the masking one from self.retry_policy.
    assert "_poll_cache_max_age" in str(exc_info.value)


@pytest.mark.asyncio
async def test_ainvoke_reraises_original_error_when_bypassing_the_guard():
    provider = object.__new__(_BrokenBypassMeta)
    step = Step(provider="broken-bypass", model="m", prompt="hi")

    with pytest.raises(AttributeError) as exc_info:
        await provider.ainvoke(step)

    assert "_poll_cache_max_age" in str(exc_info.value)


def test_declaring_base_init_done_true_does_not_bypass_the_guard():
    """The guard's signal is an identity-checked private token, not a plain
    bool — so a subclass can't defeat it by declaring the old-style attribute
    name (or any other truthy value) under a name it guesses at."""

    class Sneaky(SyncProvider):
        name = "sneaky"
        _base_init_done = True  # not the real signal; must not bypass anything
        _base_init_token = True  # guessed attempt at the real attribute name

        def __init__(self):
            pass  # deliberately skips super().__init__()

        def generate(self, step: Step, config=None) -> Step:
            return step

    with pytest.raises(TypeError) as exc_info:
        Sneaky()

    assert "super().__init__()" in str(exc_info.value)


def test_dataclass_mixin_base_does_not_misattribute_cause_to_the_subclass():
    """dataclasses.is_dataclass(cls) is true for any class with a dataclass
    ancestor anywhere in the MRO. The guard must check whether *this* class
    was decorated, not whether some unrelated mixin base was, so the message
    doesn't send the reader looking for a decorator that isn't there."""

    @dataclass
    class _DataclassMixin:
        unrelated: int = 1

    class SubclassNotItselfADataclass(SyncProvider, _DataclassMixin):
        name = "not-a-dataclass-itself"

        def __init__(self):
            pass  # forgot super().__init__() -- no @dataclass on THIS class

        def generate(self, step: Step, config=None) -> Step:
            return step

    with pytest.raises(TypeError) as exc_info:
        SubclassNotItselfADataclass()

    message = str(exc_info.value)
    assert "super().__init__()" in message
    # The message may mention @dataclass generically as a common cause to
    # check for, but must not assert that THIS class is the one decorated —
    # that would be false and send the reader looking for a decorator that
    # isn't on SubclassNotItselfADataclass.
    assert "SubclassNotItselfADataclass is decorated with @dataclass" not in message


def test_message_does_not_blame_a_specific_init_that_is_not_the_real_cause():
    """A subclass of an already-broken @dataclass provider, with NO new
    __init__ of its own, must not get a message asserting ITS __init__ never
    called super() -- it has no __init__ at all; it inherited the broken one.
    (Regression for a message that was previously provably wrong: even a
    Child.__init__ that *does* call super().__init__() still fails here,
    because super().__init__() resolves to the dataclass-generated __init__
    on the broken parent, which never forwards to BaseProvider.__init__ --
    so telling the user to "add super().__init__() to Child" is not just
    unhelpful, it's advice that doesn't fix anything.)"""

    @dataclass
    class _BrokenDataclassAncestor(SyncProvider):
        api_key: str = "k"

        @property
        def name(self):
            return "broken-ancestor"

        def generate(self, step: Step, config=None) -> Step:
            return step

    class Child(_BrokenDataclassAncestor):
        pass

    with pytest.raises(TypeError) as exc_info:
        Child()
    message = str(exc_info.value)
    assert "Child.__init__ never called super().__init__()" not in message

    # Confirm the guard's OWN general advice ("make sure each __init__ in
    # the hierarchy calls super().__init__()") is not itself falsified: even
    # adding a Child.__init__ that calls super().__init__() still fails,
    # because the break is in the inherited dataclass-generated __init__,
    # further up the chain -- this is exactly why the message must not
    # single out Child's own __init__ as the fix.
    class ChildWithSuperCall(_BrokenDataclassAncestor):
        def __init__(self):
            super().__init__()

    with pytest.raises(TypeError):
        ChildWithSuperCall()


class _BuggyRetryPolicyProvider(SyncProvider):
    """A correctly-constructed provider (super().__init__() ran fine) whose
    own retry_policy override has an unrelated bug. This must NOT be treated
    as a guard-bypass case: invoke() should return a normally-failed step
    through the standard bookkeeping, not swallow the real AttributeError and
    re-raise a stale, unrelated exception."""

    name = "buggy-retry-policy"

    @property
    def retry_policy(self):
        return self._this_attribute_does_not_exist  # genuine provider bug

    def generate(self, step: Step, config=None) -> Step:
        raise ProviderError("upstream failure")


def test_genuine_retry_policy_bug_in_a_correctly_constructed_provider_is_not_masked():
    provider = _BuggyRetryPolicyProvider()
    assert provider._base_init_token is not None  # sanity: guard passed normally

    step = Step(provider="buggy-retry-policy", model="m", prompt="hi")

    # Must raise the REAL bug (AttributeError from the broken retry_policy
    # override), not something derived from the ProviderError raised inside
    # generate() -- and must not silently skip invoke()'s failure bookkeeping.
    with pytest.raises(AttributeError) as exc_info:
        provider.invoke(step)

    assert "_this_attribute_does_not_exist" in str(exc_info.value)


@pytest.mark.asyncio
async def test_genuine_retry_policy_bug_in_a_correctly_constructed_provider_is_not_masked_async():
    provider = _BuggyRetryPolicyProvider()
    step = Step(provider="buggy-retry-policy", model="m", prompt="hi")

    with pytest.raises(AttributeError) as exc_info:
        await provider.ainvoke(step)

    assert "_this_attribute_does_not_exist" in str(exc_info.value)
