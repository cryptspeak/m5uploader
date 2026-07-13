"""Local disk cache of firmware cover images (see gui.py's `_fetch_cover`),
so re-opening the Browse Firmware / My Firmware tabs doesn't re-download
every cover image on every run - only the in-memory Tk PhotoImage cache
(`App._cover_cache`) was persistent-within-a-run before; this persists
across runs too.

Cache entries are keyed by a hash of the cover identifier, never the
identifier itself. The identifier comes from the server (part of the
catalog JSON) and must never be used directly as a filesystem path -
hashing it sidesteps path traversal entirely as a class of bug, rather
than relying on the identifier happening to look safe today.
"""

import hashlib
import os
import stat

from . import config

CACHE_DIR = config.CACHE_DIR / "covers"
MAX_TOTAL_BYTES = 150_000_000  # 150 MB - covers are small (<=500KB each, see imaging.py)


def _cache_path(cover: str):
    digest = hashlib.sha256(cover.encode("utf-8")).hexdigest()
    return CACHE_DIR / digest


def load_cached_image(cover: str):
    """Returns the cached image bytes, or None if not cached / unreadable.
    Never raises."""
    path = _cache_path(cover)
    if not path.exists():
        return None
    try:
        return path.read_bytes()
    except OSError:
        return None


def save_cached_image(cover: str, data: bytes) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        os.chmod(CACHE_DIR, stat.S_IRWXU)
    path = _cache_path(cover)
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_bytes(data)
        if os.name == "posix":
            os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        return
    _evict_if_needed()


def _evict_if_needed() -> None:
    try:
        entries = sorted(CACHE_DIR.iterdir(), key=lambda p: p.stat().st_mtime)
        total = sum(p.stat().st_size for p in entries if p.is_file())
    except OSError:
        return
    i = 0
    while total > MAX_TOTAL_BYTES and i < len(entries):
        try:
            total -= entries[i].stat().st_size
            entries[i].unlink()
        except OSError:
            pass
        i += 1
