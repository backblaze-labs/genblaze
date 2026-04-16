"""Tests for secret redaction in provider error messages."""

from genblaze_core.providers.base import _sanitize_error


class TestSecretRedaction:
    """Verify that real secrets are redacted and false positives are not."""

    def test_replicate_token_redacted(self):
        msg = "Auth failed: r8_abcdefghij1234567890"
        result = _sanitize_error(msg)
        assert "r8_" not in result
        assert "[REDACTED]" in result

    def test_openai_key_redacted(self):
        msg = "Invalid key: sk-abcdefghijklmnopqrstuvwxyz"
        result = _sanitize_error(msg)
        assert "sk-" not in result
        assert "[REDACTED]" in result

    def test_bearer_token_redacted(self):
        msg = "Header: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
        result = _sanitize_error(msg)
        assert "eyJ" not in result
        assert "[REDACTED]" in result

    def test_token_auth_redacted(self):
        msg = "Header: Token abcdefghijklmnopqrstuvwxyz1234"
        result = _sanitize_error(msg)
        assert "abcdefghij" not in result
        assert "[REDACTED]" in result

    def test_api_key_equals_redacted(self):
        msg = "Request failed: api_key=sk_live_abcdefghijklmnopqrstuvwx"
        result = _sanitize_error(msg)
        assert "sk_live" not in result
        assert "[REDACTED]" in result

    def test_api_key_colon_redacted(self):
        msg = "Config: api-key: my_secret_key_value_1234567890"
        result = _sanitize_error(msg)
        assert "my_secret" not in result
        assert "[REDACTED]" in result

    def test_keyboard_false_positive_not_redacted(self):
        """Words starting with 'key' like 'keyboard' should NOT be redacted."""
        msg = "keyboardabcdefghijklmnopqrstuvwxyz is not a secret"
        result = _sanitize_error(msg)
        assert result == msg

    def test_keyword_false_positive_not_redacted(self):
        msg = "keyword_for_search_in_the_database_query"
        result = _sanitize_error(msg)
        assert result == msg

    def test_truncation(self):
        msg = "x" * 600
        result = _sanitize_error(msg)
        assert len(result) < 600
        assert "...(truncated)" in result

    def test_google_api_key_redacted(self):
        msg = "Error: AIzaSyA1234567890abcdefghijklmnopqrstuv"
        result = _sanitize_error(msg)
        assert "AIza" not in result
        assert "[REDACTED]" in result

    def test_aws_access_key_redacted(self):
        msg = "Credential: AKIAIOSFODNN7EXAMPLE"
        result = _sanitize_error(msg)
        assert "AKIA" not in result
        assert "[REDACTED]" in result

    def test_anthropic_key_redacted(self):
        msg = "Auth failed: sk-ant-api03-abcdefghijklmnopqrstuvwxyz"
        result = _sanitize_error(msg)
        assert "sk-ant-" not in result
        assert "[REDACTED]" in result

    def test_clean_message_unchanged(self):
        msg = "Connection refused: timeout after 30s"
        assert _sanitize_error(msg) == msg
