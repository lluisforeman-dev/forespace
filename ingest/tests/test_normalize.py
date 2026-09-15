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
