"""Passive "an update is available" notice - no auto-updater by design.

This module only ever answers the question "is there a newer release?"
and hands back a URL to the GitHub Releases page. It never downloads,
installs, or executes anything on its own - the user reviews and
downloads the update themselves, in their own browser, the same way
registration is handled in gui.py. That's a deliberate security choice,
not an oversight: an auto-updater is itself a remote-code-execution
vector (a compromised release server or an MITM'd update channel could
push and run arbitrary code), and this project's whole reason to exist is
a smaller attack surface than the official app it replaces.

Checks are cached locally and rate-limited to once a day by default so
this never comes close to GitHub's unauthenticated 60-requests/hour
limit, and any failure (no network, GitHub unreachable, unexpected
response shape) is swallowed silently - a failed update check must never
block startup or look like an application error.
"""

import json
import os
import stat
import time
from dataclasses import dataclass

import requests
from packaging.version import InvalidVersion, Version

from . import config

RELEASES_API_URL = "https://api.github.com/repos/cryptspeak/m5uploader/releases/latest"
STATE_FILE = config.CACHE_DIR / "update_check.json"
DEFAULT_MAX_AGE = 24 * 60 * 60  # 1 day


@dataclass(frozen=True)
class UpdateInfo:
    tag: str
    url: str


def _load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        data = json.loads(STATE_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_state(state: dict) -> None:
    config.ensure_dirs()
    tmp = STATE_FILE.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(state))
        if os.name == "posix":
            os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
        os.replace(tmp, STATE_FILE)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass


def _fetch_latest_release():
    """Returns (tag, html_url) from the GitHub API, or None on any
    failure. Never raises."""
    try:
        resp = requests.get(
            RELEASES_API_URL,
            headers={"Accept": "application/vnd.github+json"},
            timeout=config.REQUEST_TIMEOUT,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        tag = data.get("tag_name")
        url = data.get("html_url")
        if not tag or not url:
            return None
        return tag, url
    except (requests.RequestException, ValueError):
        return None


def _is_newer(remote_tag: str, current_version: str) -> bool:
    try:
        return Version(remote_tag.lstrip("v")) > Version(current_version.lstrip("v"))
    except InvalidVersion:
        return False


def check_for_update(current_version: str, *, force: bool = False):
    """Returns an UpdateInfo if a newer release is available, else None.
    Never raises - any network/parse failure is treated the same as "no
    update available right now". Rate-limited to once per DEFAULT_MAX_AGE
    unless `force` is set (e.g. a manual "Check for updates" action)."""
    state = _load_state()
    last_checked = state.get("last_checked")
    if not force and isinstance(last_checked, (int, float)) and time.time() - last_checked < DEFAULT_MAX_AGE:
        tag, url = state.get("latest_tag"), state.get("html_url")
        if tag and url and _is_newer(tag, current_version):
            return UpdateInfo(tag=tag, url=url)
        return None

    result = _fetch_latest_release()
    if result is None:
        return None
    tag, url = result
    _save_state({"last_checked": time.time(), "latest_tag": tag, "html_url": url})

    if _is_newer(tag, current_version):
        return UpdateInfo(tag=tag, url=url)
    return None
