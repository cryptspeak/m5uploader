"""Small persisted user preferences for m5uploader.

- count_downloads: whether firmware downloads should count toward the
  public download total shown in the catalog - the same counter the
  official M5Burner app updates via `api.py`'s `ping_firmware_download()`
  on every download. m5uploader never calls that endpoint unless this is
  explicitly turned on; off by default.
- dark_mode: light/dark theme choice, so it survives restarts instead of
  resetting to light every launch.

This is a preference, not disposable data, so it lives in CONFIG_DIR
(alongside the session file) rather than CACHE_DIR - a "clear cache"
tool shouldn't silently reset it.
"""

import json
import os
import stat

from . import config

SETTINGS_FILE = config.CONFIG_DIR / "settings.json"

_DEFAULTS = {"count_downloads": False, "dark_mode": False}


def _load() -> dict:
    if not SETTINGS_FILE.exists():
        return dict(_DEFAULTS)
    try:
        data = json.loads(SETTINGS_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return dict(_DEFAULTS)
    if not isinstance(data, dict):
        return dict(_DEFAULTS)
    merged = dict(_DEFAULTS)
    merged.update({k: data[k] for k in _DEFAULTS if k in data})
    return merged


def get_count_downloads() -> bool:
    return bool(_load().get("count_downloads", False))


def set_count_downloads(value: bool) -> None:
    _set("count_downloads", value)


def get_dark_mode() -> bool:
    return bool(_load().get("dark_mode", False))


def set_dark_mode(value: bool) -> None:
    _set("dark_mode", value)


def _set(key: str, value: bool) -> None:
    config.ensure_dirs()
    data = _load()
    data[key] = bool(value)
    SETTINGS_FILE.write_text(json.dumps(data))
    if os.name == "posix":
        os.chmod(SETTINGS_FILE, stat.S_IRUSR | stat.S_IWUSR)
