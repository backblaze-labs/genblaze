"""Lint-style guard: no hardcoded USD rate tables in connector source files.

The 0.3.0 contract mandates zero `_RATES`-style constants in any
`libs/connectors/*/genblaze_*/` module. This test enforces that invariant so
new connectors (including standalone `chat()` helpers) cannot reintroduce
static pricing tables without failing CI.

Detection is two-step to stay precise:
1. Find identifiers assigned a dict literal (``NAME = {`` / ``NAME: T = {``).
2. Flag the name only if one of its words is a money term (rate(s), price(s),
   pricing, cost(s)).

Requiring a dict literal on the RHS is what separates a rate *table* from a
benign scalar like ``_rate_limit_max = 5`` or the public ``cost_usd`` field;
the word match (not a substring match) keeps names like ``_operate`` or
``sample_rate`` from tripping the guard. See the fixtures below — they pin
both halves so a future edit can't silently widen or neuter the matcher.

Excluded paths:
- `tests/` subdirectories
- `__pycache__` directories
"""

from __future__ import annotations

import re
from pathlib import Path

# Root of the monorepo: libs/core/tests/unit/ is 4 levels deep.
_REPO_ROOT = Path(__file__).parents[4]
_CONNECTORS_ROOT = _REPO_ROOT / "libs" / "connectors"

# Money terms that mark an identifier as a pricing/rate table when bound to a
# dict. Matched as whole words (after splitting the identifier), never as
# substrings, so `_operate` ("rate") and `_corporate` do not trip the guard.
_MONEY_WORDS = frozenset({"rate", "rates", "price", "prices", "pricing", "cost", "costs"})

# Splits an identifier into its words, handling SCREAMING_SNAKE, snake_case, and
# camelCase (e.g. `_VEO_PER_SECOND_RATES` -> {veo, per, second, rates}).
_WORD_RE = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z]+|[a-z]+")

# An assignment whose right-hand side opens a dict literal, with an optional
# type annotation: `NAME = {`, `NAME: dict[str, float] = {`. MULTILINE so it
# matches indented class-body assignments too.
_DICT_ASSIGN_RE = re.compile(
    r"^[ \t]*(?P<name>[A-Za-z_]\w*)\s*(?::[^=\n]+)?=\s*[^\n]*\{",
    re.MULTILINE,
)


def _is_money_name(name: str) -> bool:
    """True if any word in the identifier is a pricing/rate/cost term."""
    return bool({w.lower() for w in _WORD_RE.findall(name)} & _MONEY_WORDS)


def _find_rate_tables(source: str) -> list[tuple[int, str]]:
    """Return (line_number, name) for every money-named dict assignment."""
    hits: list[tuple[int, str]] = []
    for match in _DICT_ASSIGN_RE.finditer(source):
        name = match.group("name")
        if _is_money_name(name):
            line = source[: match.start()].count("\n") + 1
            hits.append((line, name))
    return hits


def _connector_source_files() -> list[Path]:
    """Return all connector .py source files, excluding tests/ and __pycache__."""
    files: list[Path] = []
    for py in _CONNECTORS_ROOT.rglob("*.py"):
        parts = py.parts
        # Skip test directories and compiled caches.
        if any(p in ("tests", "__pycache__") for p in parts):
            continue
        files.append(py)
    return files


def test_rate_table_matcher_fixtures():
    """Pin the matcher: it must fire on rate tables and stay quiet on look-alikes.

    Guards against the matcher silently breaking (a regex that matches nothing
    would otherwise make the contract test below pass vacuously).
    """
    should_match = [
        "_RATES = {",
        "_TOKEN_RATES = {",
        "_VEO_PER_SECOND_RATES = {",  # historical google offender
        "_IMAGEN_PER_IMAGE_RATES = {",  # historical google offender
        "_token_rates = {",
        "MODEL_PRICING = {",
        "_PRICE_TABLE = {",
        "_RATES: dict[str, float] = {",
        "    _COST_PER_SEC = {",  # indented class-body assignment
    ]
    should_not_match = [
        "cost_usd = None",  # the public ChatResponse field
        "sample_rate = 16000",
        "_rate_limit_max = 5",  # scalar, not a table
        "_price = 1.0",  # scalar
        "_PRICE: float = 1.0",  # annotated scalar
        "_operate = {",  # 'rate' is a substring, not a word
        "register_pricing(slug, strategy)",  # registry call, not an assignment
    ]
    for snippet in should_match:
        assert _find_rate_tables(snippet), f"matcher missed rate table: {snippet!r}"
    for snippet in should_not_match:
        assert not _find_rate_tables(snippet), f"matcher false-positive: {snippet!r}"


def test_no_hardcoded_rate_tables_in_connectors():
    """All connector modules must be free of hardcoded USD rate dictionaries.

    Pricing must flow through the user-registered model registry
    (PricingContext / ModelSpec.pricing) for Pipeline-Step providers, or be
    computed by the caller from token counts for standalone chat() helpers —
    never a static dict baked into the module.
    """
    files = _connector_source_files()
    # Fail loud if the scan target moved or the tree is absent, so the guard
    # can never pass vacuously by silently scanning zero files.
    assert files, f"no connector source files found under {_CONNECTORS_ROOT}"

    offenders: list[str] = []
    for path in files:
        source = path.read_text(encoding="utf-8")
        rel = path.relative_to(_REPO_ROOT)
        for line, name in _find_rate_tables(source):
            offenders.append(f"{rel}:{line}: {name}")

    assert not offenders, (
        "Hardcoded USD rate tables found in connector source files. "
        "Remove the rate/price dict and return cost_usd=None; callers compute "
        "cost from token counts, or Pipeline-Step providers register rates via "
        "PricingContext / ModelSpec.pricing. Offenders:\n"
        + "\n".join(f"  - {o}" for o in offenders)
    )
