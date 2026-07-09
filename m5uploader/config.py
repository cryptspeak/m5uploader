"""Paths, hosts and constants for m5uploader.

Unlike the official M5Burner client, every host here is contacted over
HTTPS only. All hosts below were verified (2026-07-08) to serve valid
TLS certificates and respond correctly over HTTPS, so there is no
functional reason to ever fall back to plaintext HTTP.
"""

import os
import sys
from pathlib import Path

APP_NAME = "m5uploader"

UIFLOW_HOST = "https://uiflow2.m5stack.com"
BURNER_API_HOST = "https://m5burner-api.m5stack.com"
CATALOG_FIRMWARE_CDN = "https://m5burner-cdn.m5stack.com/firmware"
COVER_IMAGE_HOST = "https://m5burner.m5stack.com/cover"
SHARE_FIRMWARE_HOST = "https://m5burner.oss-cn-shenzhen.aliyuncs.com/firmware"

REQUEST_TIMEOUT = 15  # seconds


def _config_home() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = str(Path.home() / "Library" / "Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / APP_NAME


CONFIG_DIR = _config_home()
SESSION_FILE = CONFIG_DIR / "session.json"


def ensure_dirs() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        os.chmod(CONFIG_DIR, 0o700)
