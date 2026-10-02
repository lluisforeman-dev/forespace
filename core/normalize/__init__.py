# Pure functions only — no DB, no network, no LLM calls.
import re
import unicodedata

_SUFFIXES = {
    # Legal entity types
    'inc', 'llc', 'ltd', 'corp', 'co', 'gmbh', 'sa', 'sas', 'bv', 'ag', 'plc',
    'sl', 'slu', 'spa', 'nv', 'oy', 'ab',
    # Org-type words that don't distinguish entities
    'foundation', 'institute', 'institution', 'association', 'society',
    'consortium', 'authority', 'agency', 'centre', 'center',
    'hub', 'network', 'alliance', 'initiative', 'cluster', 'programme', 'program',
    # Industry descriptors
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


# ── Country normalization ────────────────────────────────────────────────────
# LLMs return headquarters_country as "US", "USA", "United States", "U.S."
# interchangeably. Store ISO 3166-1 alpha-2 so grouping, filtering and
# map rendering are deterministic. Unknown values pass through untouched —
# never guess.

_COUNTRIES = {
    # English long forms and common abbreviations
    'united states': 'US', 'united states of america': 'US', 'usa': 'US',
    'us': 'US', 'u.s.': 'US', 'u.s.a.': 'US', 'america': 'US', 'united states (usa)': 'US',
    'united kingdom': 'GB', 'uk': 'GB', 'u.k.': 'GB', 'great britain': 'GB',
    'britain': 'GB', 'england': 'GB', 'scotland': 'GB', 'wales': 'GB', 'northern ireland': 'GB',
    'united arab emirates': 'AE', 'uae': 'AE', 'u.a.e.': 'AE',
    'south korea': 'KR', 'korea': 'KR', 'republic of korea': 'KR', 'korea, south': 'KR',
    'north korea': 'KP', 'czech republic': 'CZ', 'czechia': 'CZ',
    'russia': 'RU', 'russian federation': 'RU',
    'the netherlands': 'NL', 'netherlands': 'NL', 'holland': 'NL',
    'hong kong sar': 'HK', 'hong kong': 'HK',
    'republic of ireland': 'IE', 'ireland': 'IE', 'republic of poland': 'PL',
    'federal republic of germany': 'DE',
    # English names — the common case
    'germany': 'DE', 'spain': 'ES', 'france': 'FR', 'italy': 'IT', 'portugal': 'PT',
    'belgium': 'BE', 'switzerland': 'CH', 'austria': 'AT', 'sweden': 'SE',
    'norway': 'NO', 'denmark': 'DK', 'finland': 'FI', 'poland': 'PL',
    'luxembourg': 'LU', 'greece': 'GR', 'turkey': 'TR', 'israel': 'IL',
    'india': 'IN', 'japan': 'JP', 'china': 'CN', 'taiwan': 'TW',
    'australia': 'AU', 'new zealand': 'NZ', 'canada': 'CA', 'mexico': 'MX',
    'brazil': 'BR', 'argentina': 'AR', 'chile': 'CL', 'colombia': 'CO', 'peru': 'PE',
    'south africa': 'ZA', 'singapore': 'SG', 'indonesia': 'ID', 'malaysia': 'MY',
    'thailand': 'TH', 'vietnam': 'VN', 'philippines': 'PH', 'nigeria': 'NG',
    'kenya': 'KE', 'egypt': 'EG', 'saudi arabia': 'SA', 'qatar': 'QA',
    'ukraine': 'UA', 'romania': 'RO', 'hungary': 'HU', 'bulgaria': 'BG',
    'croatia': 'HR', 'slovakia': 'SK', 'slovenia': 'SI', 'estonia': 'EE',
    'latvia': 'LV', 'lithuania': 'LT', 'iceland': 'IS',
    # Native-language names seen in European space-industry press
    'deutschland': 'DE', 'españa': 'ES', 'espana': 'ES',
    'italia': 'IT', 'nederland': 'NL', 'sverige': 'SE', 'suomi': 'FI',
    'danmark': 'DK', 'norge': 'NO', 'polska': 'PL', 'österreich': 'AT', 'osterreich': 'AT',
    'belgique': 'BE', 'belgië': 'BE', 'belgie': 'BE', 'schweiz': 'CH', 'suisse': 'CH',
    'svizzera': 'CH', '卢森堡': 'LU',
    # ISO-2 passthrough (validated)
    'fr': 'FR', 'de': 'DE', 'es': 'ES', 'it': 'IT', 'nl': 'NL', 'be': 'BE',
    'ch': 'CH', 'at': 'AT', 'se': 'SE', 'no': 'NO', 'dk': 'DK', 'fi': 'FI',
    'pl': 'PL', 'pt': 'PT', 'ie': 'IE', 'lu': 'LU', 'cz': 'CZ', 'gr': 'GR',
    'tr': 'TR', 'il': 'IL', 'in': 'IN', 'jp': 'JP', 'cn': 'CN', 'kr': 'KR',
    'au': 'AU', 'nz': 'NZ', 'ca': 'CA', 'mx': 'MX', 'br': 'BR', 'ar': 'AR',
    'za': 'ZA', 'sg': 'SG', 'tw': 'TW', 'ua': 'UA', 'ro': 'RO',
    'hu': 'HU', 'bg': 'BG', 'hr': 'HR', 'sk': 'SK', 'si': 'SI', 'ee': 'EE',
    'lv': 'LV', 'lt': 'LT', 'is': 'IS', 'sa': 'SA', 'qa': 'QA', 'eg': 'EG',
    'ng': 'NG', 'ke': 'KE', 'id': 'ID', 'my': 'MY', 'th': 'TH', 'vn': 'VN',
    'ph': 'PH', 'cl': 'CL', 'pe': 'PE', 'co': 'CO',
}


# Set of valid ISO-2 codes this system recognises — derived from the map values.
_COUNTRY_CODES = set(_COUNTRIES.values())


def normalize_country(value):
    """Normalize a country name/abbreviation to ISO 3166-1 alpha-2.

    Returns None when the value is not recognised — callers keep the original
    text rather than guessing. 2-letter inputs must be real ISO-2 codes
    (members of the map's value set), never arbitrary 2-letter strings.
    """
    if not value:
        return None
    v = str(value).strip()
    if not v:
        return None
    if len(v) == 2 and v.isalpha():
        code = v.upper()
        if code in _COUNTRY_CODES:
            return code
        return None
    key = ' '.join(v.casefold().split())
    key = key.rstrip('.').strip()
    return _COUNTRIES.get(key)
