"""Tests for genblaze_google._errors.map_google_error."""

from __future__ import annotations

from genblaze_core.models.enums import ProviderErrorCode
from genblaze_google._errors import map_google_error


def test_content_policy_safety():
    assert (
        map_google_error(Exception("PROMPT_BLOCKED_BY_SAFETY")) == ProviderErrorCode.CONTENT_POLICY
    )


def test_content_policy_responsible_ai():
    assert (
        map_google_error(Exception("RESPONSIBLEAI policy violation"))
        == ProviderErrorCode.CONTENT_POLICY
    )


def test_content_policy_content_filter():
    assert (
        map_google_error(Exception("output was content_filter-rejected"))
        == ProviderErrorCode.CONTENT_POLICY
    )


def test_rate_limit_still_classifies():
    assert map_google_error(Exception("RESOURCE_EXHAUSTED 429")) == ProviderErrorCode.RATE_LIMIT


def test_auth_failure():
    assert map_google_error(Exception("403 permission denied")) == ProviderErrorCode.AUTH_FAILURE


def test_unknown_fallback():
    assert map_google_error(Exception("mystery failure")) == ProviderErrorCode.UNKNOWN
