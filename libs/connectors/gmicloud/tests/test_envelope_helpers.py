"""Unit tests for envelope/error helpers in ``genblaze_gmicloud._base``.

Integration tests in the provider files cover the happy path through the full
submit→poll→fetch lifecycle. These tests lock the edge cases in isolation.
"""

from __future__ import annotations

from genblaze_gmicloud._base import extract_media_url, unwrap_error_body

# --- extract_media_url ------------------------------------------------------


class TestExtractMediaUrl:
    def test_current_shape_dict(self):
        outcome = {"media_urls": [{"url": "https://x/y.mp4"}]}
        assert extract_media_url(outcome) == "https://x/y.mp4"

    def test_current_shape_list_of_strings(self):
        outcome = {"media_urls": ["https://x/y.mp4"]}
        assert extract_media_url(outcome) == "https://x/y.mp4"

    def test_legacy_video_url(self):
        assert extract_media_url({"video_url": "https://x/y.mp4"}) == "https://x/y.mp4"

    def test_legacy_image_url(self):
        assert extract_media_url({"image_url": "https://x/y.png"}) == "https://x/y.png"

    def test_legacy_audio_url(self):
        assert extract_media_url({"audio_url": "https://x/y.mp3"}) == "https://x/y.mp3"

    def test_legacy_generic_url(self):
        assert extract_media_url({"url": "https://x/y.bin"}) == "https://x/y.bin"

    def test_current_shape_preferred_over_legacy(self):
        outcome = {
            "media_urls": [{"url": "https://current/new.mp4"}],
            "video_url": "https://legacy/old.mp4",
        }
        assert extract_media_url(outcome) == "https://current/new.mp4"

    def test_thumbnail_fallback_only_for_images(self):
        outcome = {"thumbnail_image_url": "https://x/thumb.png"}
        assert extract_media_url(outcome, image_fallback=True) == "https://x/thumb.png"
        assert extract_media_url(outcome, image_fallback=False) is None

    def test_empty_outcome(self):
        assert extract_media_url({}) is None

    def test_empty_media_urls(self):
        assert extract_media_url({"media_urls": []}) is None

    def test_malformed_media_urls_entry_falls_through(self):
        # First entry is a dict without ``url`` — should fall through to legacy.
        outcome = {
            "media_urls": [{"uri": "https://wrong/key.mp4"}],
            "video_url": "https://legacy/right.mp4",
        }
        assert extract_media_url(outcome) == "https://legacy/right.mp4"

    def test_media_urls_as_non_list_ignored(self):
        outcome = {"media_urls": "https://not-a-list.mp4", "video_url": "https://ok.mp4"}
        assert extract_media_url(outcome) == "https://ok.mp4"


# --- unwrap_error_body ------------------------------------------------------


class TestUnwrapErrorBody:
    def test_gmicloud_error_shape(self):
        body = '{"error":"Backend error (400). Please try again."}'
        assert unwrap_error_body(body) == "Backend error (400). Please try again."

    def test_message_key(self):
        body = '{"message":"Quota exceeded"}'
        assert unwrap_error_body(body) == "Quota exceeded"

    def test_detail_key(self):
        body = '{"detail":"invalid parameter: aspect_ratio"}'
        assert unwrap_error_body(body) == "invalid parameter: aspect_ratio"

    def test_empty_body_returns_as_is(self):
        assert unwrap_error_body("") == ""
        assert unwrap_error_body("   ") == "   "

    def test_malformed_json_returns_as_is(self):
        body = "<html><body>502 Bad Gateway</body></html>"
        assert unwrap_error_body(body) == body

    def test_json_array_returns_as_is(self):
        body = '["nope", "also nope"]'
        assert unwrap_error_body(body) == body

    def test_json_without_known_keys_returns_as_is(self):
        body = '{"code":500,"other":"nope"}'
        assert unwrap_error_body(body) == body

    def test_non_string_error_value_returns_as_is(self):
        body = '{"error":{"nested":"object"}}'
        assert unwrap_error_body(body) == body

    def test_plain_text(self):
        assert unwrap_error_body("upstream timeout") == "upstream timeout"
