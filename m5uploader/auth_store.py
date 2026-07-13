"""Session storage for m5uploader.

Only the session token (plus email/username, for display purposes) is
ever persisted - the password is never written to disk and is discarded
as soon as the login request completes, unlike the official M5Burner
client, which round-trips the plaintext password back into the renderer
process and (per the ipc event payload) keeps it in memory/state there.

This used to deliberately avoid the OS keychain (a plain 0600-permissioned
file only), because on Linux the `keyring` package depends on a Secret
Service provider (GNOME Keyring/KWallet) being present and unlocked, which
isn't guaranteed (headless/minimal setups, different desktop
environments) - and a keyring failure can be hard to tell apart from "the
token is invalid" from here.

That risk is still real, so the fix isn't to trust keyring unconditionally
- it's to try it and verify it actually works (set a value, read it back,
delete it - not just "a backend object was constructed"), and transparently
fall back to the original plain-file behavior whenever it doesn't. Login
never breaks because of an unavailable/locked keyring; it just means the
token is stored the same way it always was. Any existing plain-file
session is opportunistically migrated into the keyring (and the plaintext
copy removed) the first time a working keyring is seen, so upgrading users
also benefit without having to log in again.
"""

import json
import os
import stat
import sys

import keyring
import keyring.errors

from . import config

_KEYRING_SERVICE = "m5uploader"
_KEYRING_TOKEN_KEY = "session_token"
_KEYRING_PROBE_KEY = "__probe__"

_keyring_usable = None  # memoized per-process; None = not probed yet


def _select_frozen_backend() -> None:
    """PyInstaller's static import analysis can miss keyring's
    entry-point-based backend discovery, which then fails in a frozen
    build even though the same code works fine from source. Explicitly
    picking the concrete platform backend sidesteps that discovery step
    entirely. Best-effort: if this fails for any reason, normal
    auto-discovery (and the probe below) still applies."""
    if not getattr(sys, "frozen", False):
        return
    try:
        if sys.platform == "darwin":
            from keyring.backends.macOS import Keyring
        elif sys.platform == "win32":
            from keyring.backends.Windows import WinVaultKeyring as Keyring
        else:
            from keyring.backends.SecretService import Keyring
        keyring.set_keyring(Keyring())
    except Exception:
        pass


_select_frozen_backend()


def _keyring_available() -> bool:
    """Cheap object introspection (e.g. checking keyring.get_keyring()'s
    class) isn't reliable - a backend can construct successfully and still
    fail on first real use (a locked Secret Service, for example). This
    actually sets, reads back, and deletes a value once per process and
    caches the result. Deliberately catches any exception, not just
    keyring.errors.KeyringError - backend-specific failures (D-Bus errors,
    permission errors, etc.) aren't guaranteed to all be KeyringError
    subclasses, and the whole point is that *no* keyring failure should
    ever be allowed to break login."""
    global _keyring_usable
    if _keyring_usable is None:
        try:
            keyring.set_password(_KEYRING_SERVICE, _KEYRING_PROBE_KEY, "x")
            ok = keyring.get_password(_KEYRING_SERVICE, _KEYRING_PROBE_KEY) == "x"
            keyring.delete_password(_KEYRING_SERVICE, _KEYRING_PROBE_KEY)
            _keyring_usable = ok
        except Exception:
            _keyring_usable = False
    return _keyring_usable


def _load() -> dict:
    if not config.SESSION_FILE.exists():
        return {}
    try:
        return json.loads(config.SESSION_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _write_file(data: dict) -> None:
    config.ensure_dirs()
    config.SESSION_FILE.write_text(json.dumps(data))
    if os.name == "posix":
        os.chmod(config.SESSION_FILE, stat.S_IRUSR | stat.S_IWUSR)


def save_session(token: str, email: str, username: str = "") -> None:
    if _keyring_available():
        try:
            keyring.set_password(_KEYRING_SERVICE, _KEYRING_TOKEN_KEY, token)
            _write_file({"email": email, "username": username, "token_in_keyring": True})
            return
        except Exception:
            pass  # fall through to plain-file storage below
    _write_file({"email": email, "username": username, "token": token})


def load_session() -> tuple:
    """Returns (token, email, username) or (None, None, None) if nothing
    is stored. A restored token is always re-validated against the server
    by the caller (M5StackAPI.validate_session()), and an invalid/missing
    token there already means "ask the user to log in again" - so a
    keyring that became unavailable or was cleared since the last run
    needs no special handling here, it just naturally produces that same,
    already-handled outcome."""
    data = _load()
    if not data:
        return None, None, None
    email, username = data.get("email"), data.get("username")

    if data.get("token_in_keyring"):
        try:
            token = keyring.get_password(_KEYRING_SERVICE, _KEYRING_TOKEN_KEY)
        except Exception:
            token = None
        return token, email, username

    # Old-format file (or keyring was never available): plaintext token.
    # Opportunistically migrate it into the keyring now that one may be
    # available, so upgrading users don't keep a plaintext copy around
    # forever just because they logged in before this existed.
    token = data.get("token")
    if token and _keyring_available():
        try:
            keyring.set_password(_KEYRING_SERVICE, _KEYRING_TOKEN_KEY, token)
            _write_file({"email": email, "username": username, "token_in_keyring": True})
        except Exception:
            pass  # keep using the plaintext copy this run; migration is retried next run
    return token, email, username


def clear_session() -> None:
    data = _load()
    if data.get("token_in_keyring"):
        try:
            keyring.delete_password(_KEYRING_SERVICE, _KEYRING_TOKEN_KEY)
        except Exception:
            pass
    if config.SESSION_FILE.exists():
        try:
            config.SESSION_FILE.unlink()
        except OSError:
            pass
