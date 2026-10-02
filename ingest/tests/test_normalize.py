"""Tests for core.normalize.normalize_name — entity resolution preprocessing."""
import pytest

from core.normalize import normalize_name


@pytest.mark.parametrize("input_name, expected", [
    # Suffix stripping (legal suffixes are in _SUFFIXES set)
    ("SpaceX Inc.", "spacex"),
    ("Rocket Lab Ltd", "rocket lab"),   # 'lab' not in suffixes, 'ltd' is → "rocket lab"
    ("Boeing Co.", "boeing"),
    # 'technologies' IS in _SUFFIXES, so it gets stripped too
    ("L3Harris Technologies, Inc.", "l3harris"),
    # 'labs' IS in _SUFFIXES
    ("Planet Labs Inc.", "planet"),
    # S.A.S. — dots become spaces, individual letters 's'/'a'/'s' are not suffixes
    ("Airbus S.A.S.", "airbus s a s"),
    # 'pbc' is NOT in _SUFFIXES → kept
    ("Planet Labs PBC", "planet labs pbc"),
    # Case folding
    ("ROCKET LAB", "rocket lab"),
    ("rocket lab", "rocket lab"),
    # Accent normalisation
    ("Société Générale", "societe generale"),
    ("Arianéspace", "arianespace"),
    # Punctuation → spaces
    ("SpaceX, Inc.", "spacex"),
    # Extra whitespace
    ("  SpaceX  Inc.  ", "spacex"),
    # Already clean
    ("spacex", "spacex"),
    # Single word (no suffix)
    ("NASA", "nasa"),
    # Suffix-only edge case — fallback returns normalised 's' rather than empty string
    ("Inc.", "inc"),
])
def test_normalize_name(input_name, expected):
    assert normalize_name(input_name) == expected


# ── normalize_country ────────────────────────────────────────────────────────

from core.normalize import normalize_country  # noqa: E402


@pytest.mark.parametrize("input_country, expected", [
    # Long forms → ISO-2
    ("United States", "US"),
    ("United States of America", "US"),
    ("USA", "US"),
    ("United Kingdom", "GB"),
    ("The Netherlands", "NL"),
    ("South Korea", "KR"),
    ("Czech Republic", "CZ"),
    # Native-language forms
    ("Deutschland", "DE"),
    ("España", "ES"),
    # Case and whitespace
    ("  france  ", "FR"),
    # Full English names resolve
    ("JAPAN", "JP"),
    ("japan", "JP"),
    ("Germany", "DE"),
    # ISO-2 passthrough, uppercased
    ("us", "US"),
    ("fr", "FR"),
    ("JP", "JP"),
    # Junk / empty / unknown
    ("", None),
    (None, None),
    ("   ", None),
    ("Atlantis", None),
])
def test_normalize_country(input_country, expected):
    assert normalize_country(input_country) == expected


def test_normalize_country_never_lowercases_iso():
    # A valid ISO-2 code stays uppercase
    assert normalize_country("de") == "DE"
    # A non-country 2-letter word is NOT mapped (not in the whitelist)
    assert normalize_country("xx") is None
