"""Tests for Unicode NFC normalization in canonical JSON."""

import unicodedata

from genblaze_core.canonical._normalize import normalize
from genblaze_core.canonical.json import canonical_hash, canonical_json


def test_nfc_normalization():
    """NFD and NFC forms of the same string should normalize identically."""
    # é as two codepoints (NFD) vs one (NFC)
    nfd = "e\u0301"  # e + combining acute
    nfc = "\u00e9"  # precomposed é
    assert nfd != nfc  # different raw strings
    assert normalize(nfd) == normalize(nfc)  # same after normalization
    assert normalize(nfd) == unicodedata.normalize("NFC", nfd)


def test_nfc_in_canonical_json():
    """Canonical JSON should produce identical output for NFD/NFC inputs."""
    data_nfd = {"prompt": "caf\u0065\u0301"}
    data_nfc = {"prompt": "caf\u00e9"}
    assert canonical_json(data_nfd) == canonical_json(data_nfc)


def test_nfc_hash_deterministic():
    """Hash should be identical regardless of Unicode normalization form."""
    h1 = canonical_hash({"text": "e\u0301"})
    h2 = canonical_hash({"text": "\u00e9"})
    assert h1 == h2
