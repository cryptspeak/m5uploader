"""Local cache of the public firmware catalog (`GET /api/firmware`, see
api.py) so switching to the Browse Firmware tab doesn't always re-fetch
the full catalog over the network.

Only the public catalog is cached - it's unauthenticated to begin with.
"My Firmware" is per-user and changes whenever the user edits it, so it's
deliberately never cached here.

Same defensive shape as auth_store.py: any missing, stale, or malformed
cache is treated as a cache miss (returns None) rather than trusted or
allowed to crash the app - a corrupt or tampered local file can only ever
cost an extra network round-trip, never bad data reaching the UI.
"""

import json
import os
import stat
import time

from . import config

CACHE_FILE = config.CACHE_DIR / "catalog_cache.json"
DEFAULT_MAX_AGE = 15 * 60  # 15 minutes


def load_cached_catalog(max_age: float = DEFAULT_MAX_AGE):
    """Returns the cached catalog (a list) if present and fresh, else
    None. Never raises."""
    if not CACHE_FILE.exists():
        return None
    try:
        data = json.loads(CACHE_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    cached_at = data.get("cached_at")
    catalog = data.get("catalog")
    if not isinstance(cached_at, (int, float)) or not isinstance(catalog, list):
        return None
    if time.time() - cached_at > max_age:
        return None
    return catalog


def cache_age(max_age: float = DEFAULT_MAX_AGE):
    """Seconds since the cache was written, or None if there's no usable
    cache. Used only to render an honest "cached Xm ago" label."""
    if not CACHE_FILE.exists():
        return None
    try:
        data = json.loads(CACHE_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    cached_at = data.get("cached_at") if isinstance(data, dict) else None
    if not isinstance(cached_at, (int, float)):
        return None
    return max(0, time.time() - cached_at)


def save_cached_catalog(catalog: list) -> None:
    config.ensure_dirs()
    payload = json.dumps({"cached_at": time.time(), "catalog": catalog})
    tmp = CACHE_FILE.with_suffix(".tmp")
    try:
        tmp.write_text(payload)
        if os.name == "posix":
            os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
        os.replace(tmp, CACHE_FILE)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
