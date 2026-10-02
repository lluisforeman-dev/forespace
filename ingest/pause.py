"""Pause/stop flags for Celery tasks.

Each named operation has its own Redis flag so individual task types
can be paused independently. A global flag pauses everything.

Usage in a task:
    from ingest.pause import is_paused
    if is_paused('supply_chain'):
        return
"""
GLOBAL_FLAG = 'eigengraph:pause:global'

# ── Cascade depth cap ────────────────────────────────────────────────────────
# How deep research_topic may cascade: depth 0 = the run you triggered;
# its auto-queued children are depth 1, their children depth 2, etc.
# Stored in Redis so it is editable live from the dashboard without a deploy.
# A Redis flush resets it to the default.
CASCADE_CAP_KEY = 'eigengraph:cascade:max_depth'
DEFAULT_CASCADE_CAP = 2

# Named operation flags — each maps to one or more task functions
OPERATIONS = {
    'research':                'eigengraph:pause:research',
    'supply_chain_extract':    'eigengraph:pause:supply_chain_extract',
    'supply_chain_classify':   'eigengraph:pause:supply_chain_classify',
    'summarise':               'eigengraph:pause:summarise',
    'classify':                'eigengraph:pause:classify',
    'assess':                  'eigengraph:pause:assess',
    'locations':               'eigengraph:pause:locations',
    'geocode':                 'eigengraph:pause:geocode',
    'crawl':                   'eigengraph:pause:crawl',
    'verify':                  'eigengraph:pause:verify',
}

# Keep backward-compat alias for old code that used PAUSE_FLAG
PAUSE_FLAG = GLOBAL_FLAG


def _redis():
    import redis as _r
    from django.conf import settings
    return _r.from_url(settings.CELERY_BROKER_URL)


def is_paused(operation: str = None) -> bool:
    """Return True if the global flag is set, or if the named operation flag is set."""
    try:
        r = _redis()
        if r.get(GLOBAL_FLAG):
            return True
        if operation and operation in OPERATIONS:
            return bool(r.get(OPERATIONS[operation]))
        return False
    except Exception:
        return False


def pause(operation: str = None):
    """Set pause flag for an operation (or global if None)."""
    key = OPERATIONS.get(operation, GLOBAL_FLAG) if operation else GLOBAL_FLAG
    _redis().set(key, '1')


def resume(operation: str = None):
    """Clear pause flag for an operation (or global if None)."""
    if operation is None:
        r = _redis()
        r.delete(GLOBAL_FLAG)
        for key in OPERATIONS.values():
            r.delete(key)
    else:
        key = OPERATIONS.get(operation, GLOBAL_FLAG)
        _redis().delete(key)


def paused_operations() -> list:
    """Return list of currently paused operation names (includes 'global')."""
    try:
        r = _redis()
        result = []
        if r.get(GLOBAL_FLAG):
            result.append('global')
        for name, key in OPERATIONS.items():
            if r.get(key):
                result.append(name)
        return result
    except Exception:
        return []


def get_cascade_cap() -> int:
    """Return the current cascade depth cap (default DEFAULT_CASCADE_CAP)."""
    try:
        raw = _redis().get(CASCADE_CAP_KEY)
        if raw is None:
            return DEFAULT_CASCADE_CAP
        return max(0, min(10, int(raw)))
    except Exception:
        return DEFAULT_CASCADE_CAP


def set_cascade_cap(depth: int) -> int:
    """Set the cascade depth cap (0 = no auto-cascade at all). Clamped to 0-10."""
    depth = max(0, min(10, int(depth)))
    _redis().set(CASCADE_CAP_KEY, depth)
    return depth
