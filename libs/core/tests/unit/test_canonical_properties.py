"""Property-based tests for canonical JSON using Hypothesis."""

import json
import math

from genblaze_core.canonical.json import canonical_hash, canonical_json
from hypothesis import given, settings
from hypothesis import strategies as st

# Strategy for JSON-compatible values (no NaN/Inf which aren't valid JSON)
json_values = st.recursive(
    st.none()
    | st.booleans()
    | st.integers(min_value=-(2**53), max_value=2**53)
    | st.floats(allow_nan=False, allow_infinity=False)
    | st.text(max_size=50),
    lambda children: (
        st.lists(children, max_size=5)
        | st.dictionaries(st.text(max_size=10), children, max_size=5)
    ),
    max_leaves=20,
)


class TestCanonicalJsonProperties:
    @given(data=json_values)
    @settings(max_examples=200)
    def test_idempotence(self, data):
        """canonical_json(parse(canonical_json(x))) == canonical_json(x)."""
        cj = canonical_json(data)
        reparsed = json.loads(cj)
        assert canonical_json(reparsed) == cj

    @given(data=json_values)
    @settings(max_examples=200)
    def test_determinism(self, data):
        """Same input always produces the same output."""
        assert canonical_json(data) == canonical_json(data)
        assert canonical_hash(data) == canonical_hash(data)

    @given(data=json_values)
    @settings(max_examples=100)
    def test_output_is_valid_json(self, data):
        """canonical_json always produces valid JSON."""
        cj = canonical_json(data)
        parsed = json.loads(cj)
        assert parsed is not None or data is None

    def test_float_round_trip(self):
        """Floats survive canonical JSON round-trip."""
        data = {"pi": 3.14159, "neg": -0.001, "zero": 0.0}
        cj = canonical_json(data)
        parsed = json.loads(cj)
        assert math.isclose(parsed["pi"], 3.14159, rel_tol=1e-9)
        assert math.isclose(parsed["neg"], -0.001, rel_tol=1e-9)

    def test_unicode_nfc_normalization(self):
        """Unicode strings should be NFC-normalized (if the normalizer handles it)."""
        import unicodedata

        # é as combining sequence (NFD) vs precomposed (NFC)
        nfd = "e\u0301"
        nfc = "\u00e9"
        assert unicodedata.normalize("NFC", nfd) == nfc

        # Both forms should produce the same canonical JSON
        data_nfd = {"key": nfd}
        data_nfc = {"key": nfc}
        assert canonical_json(data_nfd) == canonical_json(data_nfc)
