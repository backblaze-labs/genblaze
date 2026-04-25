"""Cross-connector conformance tests.

These tests parametrize over every entry-point-discovered provider and
assert the contracts on ``BaseProvider`` (``models=`` kwarg, hooks
default-callable, capability methods return correct types). A failure
here means a connector skipped or broke a contract; fix the connector,
not the test.
"""
