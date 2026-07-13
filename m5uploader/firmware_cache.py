"""Local disk cache of downloaded firmware binaries, used by the "Flash"
shortcut on a catalog entry (see gui.py `_on_flash_from_catalog`) so
re-flashing a firmware you've already downloaded once doesn't re-download
it. Every catalog version has its own server-assigned filename, so
caching by filename is safe - different versions never collide, and a
republished version under the same name would just mean re-downloading
(never a case of silently flashing stale data from cache, since a changed
file naturally gets a changed name upstream).

The ordinary, explicit "Download..." button in Browse Firmware is
unaffected by any of this - it always asks the user for a save location,
exactly as before.

Total size is capped, evicting the oldest entries first, so this can't
grow unbounded just from clicking around the catalog.
"""

import os
from pathlib import Path

from . import config

CACHE_DIR = config.CACHE_DIR / "firmware"
MAX_TOTAL_BYTES = 1_500_000_000  # 1.5 GB


def safe_filename(filename: str) -> str:
    """Never trust a server-provided filename as a path component
    directly - `.name` strips any directory separators (path traversal
    defense), and the fallback covers the (currently only theoretical)
    case of an empty/`.`/`..` result."""
    name = Path(filename).name.strip()
    return name if name and name not in (".", "..") else "firmware.bin"


def cache_path(filename: str) -> Path:
    return CACHE_DIR / safe_filename(filename)


def is_cached(filename: str) -> bool:
    return cache_path(filename).exists()


def ensure_dir() -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        os.chmod(CACHE_DIR, 0o700)


def evict_if_needed() -> None:
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
