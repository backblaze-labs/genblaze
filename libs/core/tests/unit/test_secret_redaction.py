"""Tests for secret redaction in provider error messages."""

from genblaze_core._utils import MAX_ERROR_LENGTH, TRUNCATION_MARKER, sanitize_error


def _aws_credential_value() -> str:
    return "wJalr" + "XUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"


class TestSecretRedaction:
    """Verify that real secrets are redacted and false positives are not."""

    def test_replicate_token_redacted(self):
        msg = "Auth failed: r8_abcdefghij1234567890"
        result = sanitize_error(msg)
        assert "r8_" not in result
        assert "[REDACTED]" in result

    def test_openai_key_redacted(self):
        msg = "Invalid key: sk-abcdefghijklmnopqrstuvwxyz"
        result = sanitize_error(msg)
        assert "sk-" not in result
        assert "[REDACTED]" in result

    def test_bearer_token_redacted(self):
        msg = "Header: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
        result = sanitize_error(msg)
        assert "eyJ" not in result
        assert "[REDACTED]" in result

    def test_token_auth_redacted(self):
        msg = "Header: Token abcdefghijklmnopqrstuvwxyz1234"
        result = sanitize_error(msg)
        assert "abcdefghij" not in result
        assert "[REDACTED]" in result

    def test_api_key_equals_redacted(self):
        # Stripe-shaped token assembled at runtime so the literal isn't a static
        # secret for secret-scanning / push-protection (it's only test data).
        secret = "sk_live_" + "abcdefghijklmnopqrstuvwx"
        msg = f"Request failed: api_key={secret}"
        result = sanitize_error(msg)
        assert "sk_live" not in result
        assert "[REDACTED]" in result

    def test_api_key_colon_redacted(self):
        msg = "Config: api-key: my_secret_key_value_1234567890"
        result = sanitize_error(msg)
        assert "my_secret" not in result
        assert "[REDACTED]" in result

    def test_keyboard_false_positive_not_redacted(self):
        """Words starting with 'key' like 'keyboard' should NOT be redacted."""
        msg = "keyboardabcdefghijklmnopqrstuvwxyz is not a secret"
        result = sanitize_error(msg)
        assert result == msg

    def test_keyword_false_positive_not_redacted(self):
        msg = "keyword_for_search_in_the_database_query"
        result = sanitize_error(msg)
        assert result == msg

    def test_truncation(self):
        msg = "x" * 600
        result = sanitize_error(msg)
        assert len(result) == MAX_ERROR_LENGTH + len(TRUNCATION_MARKER)
        assert result.endswith(TRUNCATION_MARKER)

    def test_google_api_key_redacted(self):
        # Google-shaped key assembled at runtime so the literal isn't a static
        # secret for secret-scanning / push-protection (it's only test data).
        secret = "AIza" + "SyA1234567890abcdefghijklmnopqrstuv"
        msg = f"Error: {secret}"
        result = sanitize_error(msg)
        assert "AIza" not in result
        assert "[REDACTED]" in result

    def test_aws_access_key_redacted(self):
        msg = "Credential: AKIAIOSFODNN7EXAMPLE"
        result = sanitize_error(msg)
        assert "AKIA" not in result
        assert "[REDACTED]" in result

    def test_anthropic_key_redacted(self):
        msg = "Auth failed: sk-ant-api03-abcdefghijklmnopqrstuvwxyz"
        result = sanitize_error(msg)
        assert "sk-ant-" not in result
        assert "[REDACTED]" in result

    def test_clean_message_unchanged(self):
        msg = "Connection refused: timeout after 30s"
        assert sanitize_error(msg) == msg

    def test_aws_secret_access_key_redacted(self):
        credential = _aws_credential_value()
        msg = f"AWS_SECRET_ACCESS_KEY={credential}"
        result = sanitize_error(msg)
        assert credential not in result
        assert "[REDACTED]" in result

    def test_aws_secret_access_key_json_redacted(self):
        credential = _aws_credential_value()
        msg = f'{{"SecretAccessKey": "{credential}"}}'
        result = sanitize_error(msg)
        assert credential not in result
        assert "[REDACTED]" in result

    def test_aws_secret_access_key_repr_redacted(self):
        credential = _aws_credential_value()
        msg = f"aws_secret_access_key='{credential}'"
        result = sanitize_error(msg)
        assert credential not in result
        assert "[REDACTED]" in result

    def test_aws_secret_access_key_bare_redacted(self):
        credential = _aws_credential_value()
        msg = f"SignatureDoesNotMatch computed with key {credential}"
        result = sanitize_error(msg)
        assert credential not in result
        assert "[REDACTED]" in result

    def test_b2_application_key_redacted(self):
        secret = "K005" + ("B2keyValue/" * 3)
        msg = f"B2 application key leaked: {secret}"
        result = sanitize_error(msg)
        assert secret not in result
        assert "[REDACTED]" in result

    def test_basic_auth_url_credentials_redacted(self):
        basic_auth_value = "pass" + "word-value-1234567890"
        msg = f"Fetch failed: https://user:{basic_auth_value}@example.com/object"
        result = sanitize_error(msg)
        assert basic_auth_value not in result
        assert "[REDACTED]example.com/object" in result

    def test_jwt_redacted(self):
        token = f"eyJ{'a' * 20}.{'b' * 20}.{'c' * 20}"
        msg = f"Provider returned token {token}"
        result = sanitize_error(msg)
        assert token not in result
        assert "[REDACTED]" in result

    def test_benign_k_token_not_redacted(self):
        token = "K123" + ("A" * 24)
        msg = f"Request id {token} was not found"
        assert sanitize_error(msg) == msg

    def test_short_userinfo_url_not_redacted(self):
        msg = "Fetch failed: https://user:id@example.com/object"
        assert sanitize_error(msg) == msg

    def test_large_error_scans_bounded_window(self):
        secret = "sk-" + ("a" * 40)
        msg = f"prefix {secret} " + ("x" * (MAX_ERROR_LENGTH * 20))
        result = sanitize_error(msg)
        assert secret not in result
        assert result.endswith(TRUNCATION_MARKER)
        assert len(result) == MAX_ERROR_LENGTH + len(TRUNCATION_MARKER)

    def test_sanitize_error_does_not_nest_truncation_marker(self):
        msg = ("x" * MAX_ERROR_LENGTH) + TRUNCATION_MARKER
        result = sanitize_error(msg)
        assert result.endswith(TRUNCATION_MARKER)
        assert result.count(TRUNCATION_MARKER) == 1
