"""Pause/stop flags for Celery tasks.

Each named operation has its own Redis flag so individual task types
can be paused independently. A global flag pauses everything.

Usage in a task:
    from ingest.pause import is_paused
    if is_paused('supply_chain'):
        return
"""
GLOBAL_FLAG = 'forespace:pause:global'

# Named operation flags — each maps to one or more task functions
OPERATIONS = {
    'research':                'forespace:pause:research',
    'supply_chain_extract':    'forespace:pause:supply_chain_extract',
    'supply_chain_classify':   'forespace:pause:supply_chain_classify',
    'summarise':               'forespace:pause:summarise',
    'classify':                'forespace:pause:classify',
    'assess':                  'forespace:pause:assess',
    'locations':               'forespace:pause:locations',
    'geocode':                 'forespace:pause:geocode',
    'crawl':                   'forespace:pause:crawl',
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
