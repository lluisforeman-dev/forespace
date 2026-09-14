# Pure functions only — no DB, no network, no LLM calls.
import re
import unicodedata

_SUFFIXES = {
    'inc', 'llc', 'ltd', 'corp', 'co', 'gmbh', 'sa', 'sas', 'bv', 'ag', 'plc',
    'technologies', 'technology', 'systems', 'solutions', 'group', 'holdings',
    'services', 'industries', 'international', 'enterprises', 'aerospace',
    'space', 'aviation', 'robotics', 'dynamics', 'labs', 'laboratory',
}


def normalize_name(name: str) -> str:
    """Produce the alias_norm form: casefolded, unpunctuated, suffix-stripped.

    Used for Level-2 exact matching and Level-3 trigram indexing.
    EntityAlias.alias_norm must be populated with the output of this function.
    """
    s = name.casefold()
    s = unicodedata.normalize('NFKD', s)
    s = ''.join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r'[^\w\s]', ' ', s)
    s = ' '.join(s.split())
    tokens = s.split()
    while tokens and tokens[-1] in _SUFFIXES:
        tokens.pop()
    return ' '.join(tokens) if tokens else s
