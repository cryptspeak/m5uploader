"""Session storage for m5uploader.

Only the session token (plus email/username, for display purposes) is
ever persisted, as a plain JSON file with 0600 permissions - the
password is never written to disk and is discarded as soon as the
login request completes, unlike the official M5Burner client, which
round-trips the plaintext password back into the renderer process and
(per the ipc event payload) keeps it in memory/state there.

Deliberately not using an OS keychain (e.g. via the `keyring` package):
on Linux in particular it depends on a Secret Service provider (GNOME
Keyring/KWallet) being present and unlocked, which isn't guaranteed
(headless/minimal setups, different desktop environments) and adds a
failure mode that's hard to diagnose from the app's side - `keyring`
silently falling back to a different backend than expected, or a
locked/unavailable service returning stale or empty data, can look
identical to "the token is invalid" from here. A single, plain,
0600-permissioned file is simpler and its failure modes are visible
(file exists or it doesn't; its contents parse or they don't).
"""

import json
import os
import stat

from . import config


def _load() -> dict:
    if not config.SESSION_FILE.exists():
        return {}
    try:
        return json.loads(config.SESSION_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def save_session(token: str, email: str, username: str = "") -> None:
    config.ensure_dirs()
    config.SESSION_FILE.write_text(json.dumps({"token": token, "email": email, "username": username}))
    if os.name == "posix":
        os.chmod(config.SESSION_FILE, stat.S_IRUSR | stat.S_IWUSR)


def load_session() -> tuple:
    """Returns (token, email, username) or (None, None, None) if nothing is stored."""
    data = _load()
    return data.get("token"), data.get("email"), data.get("username")


def clear_session() -> None:
    if config.SESSION_FILE.exists():
        try:
            config.SESSION_FILE.unlink()
        except OSError:
            pass
