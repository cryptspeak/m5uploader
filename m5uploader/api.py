"""M5Stack account / firmware-catalog API client.

Reimplements (over HTTPS only, with TLS verification on) the endpoints
the official M5Burner Electron app talks to. Login and the firmware
catalog were confirmed from the app's main process (packages/auth.js,
share.js, download.js). The publish/upload endpoint below was confirmed
from the app's renderer bundle (packages/view/main.*.js inside the
installed app's asar, not the desktop app's own git repo) rather than
guessed:

    GET  https://m5burner-api.m5stack.com/api/firmware
        -> full public firmware catalog as a JSON array. No auth - do
        not use this to check whether a token is still valid.
    GET  https://uiflow2.m5stack.com/api/v1/device/list
        -> requires the auth cookie; 200 means the token is still good.
        Same endpoint the original app used for this
        (`validateLoginState` in packages/auth.js).
    GET  https://m5burner-api.m5stack.com/api/admin/firmware?username={u}
        -> firmware entries authored by {u} (`getOwnFirmware`).
    POST https://m5burner-api.m5stack.com/api/admin/firmware
        multipart/form-data: name, description, category, author,
        version, github, cover (file), firmware (file) -> creates a new
        firmware entry, OR adds a new version to an existing one of
        yours if `name` matches exactly (`publishFirmware`). The
        official app's own dialog reuses this one endpoint for both
        cases (its `mode` is "create" vs "append", but it's the same
        HTTP call either way) - there is no separate "add a version"
        endpoint. `publish_firmware()` below is that same call; to add
        a version, call it again with the same `name`.
    PUT  https://m5burner-api.m5stack.com/api/admin/firmware/{fid}/version/{file}
        multipart/form-data, same fields as above, cover/firmware
        optional -> edits an existing version's metadata/file in place
        (`updateFirmware`). Different from the above: this changes a
        version that already exists, it doesn't add a new one.
    PUT  https://m5burner-api.m5stack.com/api/admin/firmware/{fid}/publish/{file}/{0|1}
        -> sets a version's public/draft visibility
        (`setPublishFirmware`/`setUnPublishFirmware`).
    POST https://m5burner-api.m5stack.com/api/admin/firmware/remove/{fid}
        json body {"version": "<version string>"} -> deletes one version
        (`removeOwnFirmware`).
    POST https://m5burner-api.m5stack.com/api/admin/firmware/share/{fid}/{file}
        -> creates a share code, response `{"data": {"code": "..."}}`
        (`getShareCode`).
    PUT  https://m5burner-api.m5stack.com/api/admin/firmware/share/{code}
        -> revokes a share code. Note the path segment is the share
        *code* itself, not a fid (`revokeShareCode`).

Despite the "admin" path segment, these are the exact URLs, HTTP
methods, and payload shapes the official app's own "Publish/Edit/Remove
Firmware" dialogs use, read from that renderer bundle - that part is
confirmed, not guessed.

Auth for this whole `/api/admin/firmware*` family is NOT a cookie and
NOT `Authorization: Bearer` - it's the session token sent as a bare,
non-standard request header literally named `m5_auth_token` (e.g.
`m5_auth_token: <token>`, no `Cookie:` prefix, no scheme). This isn't
visible in the renderer's own source (Angular's `HttpClient` calls here
attach no explicit header at all in the code - see `getOwnFirmware()`
et al - so it must be added by an HTTP interceptor elsewhere in the
bundle, or the bundle analyzed was stale) and was NOT something static
analysis or plausible-header-guessing turned up; it was confirmed
2026-07-08 via a real mitmproxy capture of the official app's own
traffic while logged into a real account (`GET /api/admin/firmware`
succeeded with exactly that header) and independently reproduced here
with `requests`. `_burner_auth_headers()` below implements this; it is
distinct from `_auth_headers()`, which is the `Cookie: m5_auth_token=`
form that `uiflow2.m5stack.com` (login/`validate_session()`) wants -
the two hosts use two different auth conventions for the same token
value, confirmed working independently for each.

Registration is intentionally not implemented here - out of scope for
this tool.
"""

import io
from pathlib import Path

import requests

from . import config
from .imaging import compress_cover


class APIError(Exception):
    def __init__(self, message: str, status_code: int = None):
        super().__init__(message)
        self.status_code = status_code


class M5StackAPI:
    def __init__(self):
        self.session = requests.Session()
        self.token = None
        self.username = None

    # -- session -----------------------------------------------------

    def set_token(self, token: str) -> None:
        self.token = token

    def _auth_headers(self) -> dict:
        """For uiflow2.m5stack.com only (login/session-validation) - it
        wants the token as an actual `Cookie: m5_auth_token=...` header,
        confirmed working via `validate_session()` below."""
        if not self.token:
            raise APIError("Not logged in")
        return {"Cookie": f"m5_auth_token={self.token}"}

    def _burner_auth_headers(self) -> dict:
        """For m5burner-api.m5stack.com/api/admin/* only - it wants the
        token as a bare, non-standard header literally named
        `m5_auth_token`, not a Cookie and not `Authorization: Bearer`.
        Confirmed 2026-07-08 from a real mitmproxy capture of the
        official app (`GET /api/admin/firmware` succeeded with
        `m5_auth_token: <token>` as a plain request header) - this
        replaces an earlier, wrong assumption that it took the same
        Cookie form as uiflow2."""
        if not self.token:
            raise APIError("Not logged in")
        return {"m5_auth_token": self.token}

    # -- auth ----------------------------------------------------------

    def login(self, email: str, password: str) -> dict:
        try:
            resp = self.session.post(
                f"{config.UIFLOW_HOST}/api/v1/account/login",
                json={"email": email, "password": password},
                timeout=config.REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            raise APIError(f"Network error during login: {exc}") from exc

        if resp.status_code != 200:
            raise APIError(
                f"Login failed (HTTP {resp.status_code})", resp.status_code
            )

        token = resp.cookies.get("m5_auth_token")
        if not token:
            raise APIError("Login response did not include a session token")

        data = resp.json().get("data", {})
        self.token = token
        self.username = data.get("username")
        return {"token": token, "username": self.username}

    def validate_session(self) -> bool:
        """Check the current token against an endpoint that actually
        requires auth. `list_firmware()` is the public catalog - it
        succeeds with no token at all, so it can't be used to tell a
        valid session from an expired/garbage one (this used to call
        that by mistake, which is why a stale restored token looked
        "logged in" right up until the first real authenticated call).
        `GET /api/v1/device/list` is the same endpoint the original
        M5Burner app used for this (`validateLoginState` in auth.js)."""
        if not self.token:
            return False
        try:
            resp = self.session.get(
                f"{config.UIFLOW_HOST}/api/v1/device/list",
                headers=self._auth_headers(),
                timeout=config.REQUEST_TIMEOUT,
            )
        except requests.RequestException:
            return False
        return resp.status_code == 200

    def logout(self) -> None:
        self.token = None
        self.username = None

    # -- firmware catalog (browse) ---------------------------------------

    def list_firmware(self) -> list:
        try:
            resp = self.session.get(
                f"{config.BURNER_API_HOST}/api/firmware",
                timeout=config.REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            raise APIError(f"Network error fetching firmware catalog: {exc}") from exc
        if resp.status_code != 200:
            raise APIError(f"Failed to fetch firmware catalog (HTTP {resp.status_code})", resp.status_code)
        try:
            return resp.json()
        except ValueError as exc:
            raise APIError("Firmware catalog response was not valid JSON") from exc

    @staticmethod
    def firmware_download_url(filename: str) -> str:
        return f"{config.CATALOG_FIRMWARE_CDN}/{filename}"

    @staticmethod
    def cover_image_url(cover: str) -> str:
        return f"{config.COVER_IMAGE_HOST}/{cover}"

    def download_firmware(self, filename: str, dest_path, progress_cb=None) -> None:
        self._download(self.firmware_download_url(filename), dest_path, progress_cb)

    def fetch_cover_image(self, cover: str) -> bytes:
        try:
            resp = self.session.get(self.cover_image_url(cover), timeout=config.REQUEST_TIMEOUT)
        except requests.RequestException as exc:
            raise APIError(f"Network error fetching cover image: {exc}") from exc
        if resp.status_code != 200:
            raise APIError(f"Failed to fetch cover image (HTTP {resp.status_code})", resp.status_code)
        return resp.content

    def ping_firmware_download(self, fid: str) -> None:
        """Opt-in only; the official app calls this unconditionally as
        telemetry on every firmware download. We never call it unless a
        caller explicitly does so."""
        try:
            self.session.post(
                f"{config.BURNER_API_HOST}/api/admin/firmware/download/{fid}",
                timeout=config.REQUEST_TIMEOUT,
            )
        except requests.RequestException:
            pass

    # -- firmware (share code / user upload) -----------------------------

    def resolve_share_code(self, code: str) -> str:
        resp = self.session.get(
            f"{config.BURNER_API_HOST}/api/firmware/share/{code}",
            timeout=config.REQUEST_TIMEOUT,
        )
        if resp.status_code != 200:
            raise APIError(f"Invalid or unknown share code (HTTP {resp.status_code})", resp.status_code)
        try:
            return resp.json()["data"]["file"]
        except (KeyError, ValueError) as exc:
            raise APIError("Unexpected response shape resolving share code") from exc

    def download_share_firmware(self, filename: str, dest_path, progress_cb=None) -> None:
        self._download(f"{config.SHARE_FIRMWARE_HOST}/{filename}", dest_path, progress_cb)

    def publish_firmware(
        self,
        file_path: str,
        name: str,
        description: str = "",
        category: str = "",
        version: str = "",
        github: str = "",
        cover_path: str = None,
    ) -> dict:
        if not self.token:
            raise APIError("Not logged in")

        fields = {
            "name": name,
            "description": description,
            "category": category,
            "author": self.username or "",
            "version": version,
            "github": github,
        }

        opened = []
        try:
            files = {"firmware": (Path(file_path).name, open(file_path, "rb"), "application/octet-stream")}
            opened.append(files["firmware"][1])
            if cover_path:
                cover_bytes, cover_name, cover_mime = compress_cover(cover_path)
                files["cover"] = (cover_name, io.BytesIO(cover_bytes), cover_mime)

            resp = self.session.post(
                f"{config.BURNER_API_HOST}/api/admin/firmware",
                data=fields,
                files=files,
                headers=self._burner_auth_headers(),
                timeout=config.REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            raise APIError(f"Network error during publish: {exc}") from exc
        finally:
            for f in opened:
                f.close()

        if resp.status_code not in (200, 201):
            raise APIError(f"Publish failed (HTTP {resp.status_code}): {resp.text[:200]}", resp.status_code)
        try:
            return resp.json()
        except ValueError:
            return {}

    def update_firmware(
        self,
        fid: str,
        version_file: str,
        name: str,
        description: str = "",
        category: str = "",
        version: str = "",
        github: str = "",
        file_path: str = None,
        cover_path: str = None,
    ) -> dict:
        if not self.token:
            raise APIError("Not logged in")

        # This endpoint only accepts multipart/form-data - confirmed from a
        # real capture of the official app, which sends even the plain text
        # fields (name/description/etc.) as individual multipart parts, not
        # a plain urlencoded body, on every call including metadata-only
        # edits. `requests` only switches to multipart when `files` is
        # non-empty, so the plain-text fields are folded into `files` too
        # (the standard `requests` idiom for a text field in a multipart
        # body: `(None, value)`) rather than passed via `data=` - that way
        # `files` is never empty and the request is always multipart, even
        # when neither firmware nor cover is being replaced. Sending a
        # non-multipart body here previously caused the server to never
        # respond at all (a read timeout, not an error response) rather
        # than reject it.
        files = {
            "name": (None, name),
            "description": (None, description),
            "category": (None, category),
            "author": (None, self.username or ""),
            "version": (None, version),
            "github": (None, github),
        }

        opened = []
        try:
            if file_path:
                files["firmware"] = (Path(file_path).name, open(file_path, "rb"), "application/octet-stream")
                opened.append(files["firmware"][1])
            if cover_path:
                cover_bytes, cover_name, cover_mime = compress_cover(cover_path)
                files["cover"] = (cover_name, io.BytesIO(cover_bytes), cover_mime)

            resp = self.session.put(
                f"{config.BURNER_API_HOST}/api/admin/firmware/{fid}/version/{version_file}",
                files=files,
                headers=self._burner_auth_headers(),
                timeout=config.REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            raise APIError(f"Network error during update: {exc}") from exc
        finally:
            for f in opened:
                f.close()

        if resp.status_code not in (200, 201):
            raise APIError(f"Update failed (HTTP {resp.status_code}): {resp.text[:200]}", resp.status_code)
        try:
            return resp.json()
        except ValueError:
            return {}

    def list_own_firmware(self) -> list:
        if not self.username:
            raise APIError("Not logged in")
        try:
            resp = self.session.get(
                f"{config.BURNER_API_HOST}/api/admin/firmware",
                params={"username": self.username},
                headers=self._burner_auth_headers(),
                timeout=config.REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            raise APIError(f"Network error fetching your firmware: {exc}") from exc
        if resp.status_code != 200:
            raise APIError(
                f"Failed to fetch your firmware (HTTP {resp.status_code}): {resp.text[:200]}", resp.status_code
            )
        try:
            data = resp.json()
        except ValueError as exc:
            raise APIError("Own-firmware response was not valid JSON") from exc
        return data.get("data", data) if isinstance(data, dict) else data

    def remove_own_firmware(self, fid: str, version: str) -> None:
        try:
            resp = self.session.post(
                f"{config.BURNER_API_HOST}/api/admin/firmware/remove/{fid}",
                json={"version": version},
                headers=self._burner_auth_headers(),
                timeout=config.REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            raise APIError(f"Network error during remove: {exc}") from exc
        if resp.status_code != 200:
            raise APIError(f"Remove failed (HTTP {resp.status_code}): {resp.text[:200]}", resp.status_code)

    def set_publish_state(self, fid: str, version_file: str, published: bool) -> None:
        state = 1 if published else 0
        try:
            resp = self.session.put(
                f"{config.BURNER_API_HOST}/api/admin/firmware/{fid}/publish/{version_file}/{state}",
                json={},
                headers=self._burner_auth_headers(),
                timeout=config.REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            raise APIError(f"Network error changing visibility: {exc}") from exc
        if resp.status_code != 200:
            raise APIError(f"Visibility change failed (HTTP {resp.status_code}): {resp.text[:200]}", resp.status_code)

    def create_share_code(self, fid: str, version_file: str) -> str:
        try:
            resp = self.session.post(
                f"{config.BURNER_API_HOST}/api/admin/firmware/share/{fid}/{version_file}",
                json={},
                headers=self._burner_auth_headers(),
                timeout=config.REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            raise APIError(f"Network error creating share code: {exc}") from exc
        if resp.status_code != 200:
            raise APIError(f"Failed to create share code (HTTP {resp.status_code}): {resp.text[:200]}", resp.status_code)
        try:
            return resp.json()["data"]["code"]
        except (KeyError, ValueError) as exc:
            raise APIError("Unexpected response shape creating share code") from exc

    def revoke_share_code(self, code: str) -> str:
        """Confirmed from a real capture: this endpoint doesn't just
        invalidate `code` - the response is the same
        `{"data": {"code": "..."}}` shape as `create_share_code`, with a
        *different* code than the one passed in. It rotates the share
        code rather than disabling sharing outright: the old code stops
        working, but a new one is immediately live. Returns that new
        code so the caller can show it - discarding it would leave the
        firmware still shareable under a code the user is never shown."""
        try:
            resp = self.session.put(
                f"{config.BURNER_API_HOST}/api/admin/firmware/share/{code}",
                json={},
                headers=self._burner_auth_headers(),
                timeout=config.REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            raise APIError(f"Network error revoking share code: {exc}") from exc
        if resp.status_code != 200:
            raise APIError(f"Failed to revoke share code (HTTP {resp.status_code}): {resp.text[:200]}", resp.status_code)
        try:
            return resp.json()["data"]["code"]
        except (KeyError, ValueError) as exc:
            raise APIError("Unexpected response shape revoking share code") from exc

    # -- internal --------------------------------------------------------

    def _download(self, url: str, dest_path, progress_cb=None) -> None:
        try:
            with self.session.get(url, stream=True, timeout=config.REQUEST_TIMEOUT) as resp:
                if resp.status_code != 200:
                    raise APIError(f"Download failed (HTTP {resp.status_code})", resp.status_code)
                total = int(resp.headers.get("content-length", 0)) or None
                received = 0
                with open(dest_path, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=65536):
                        if not chunk:
                            continue
                        f.write(chunk)
                        received += len(chunk)
                        if progress_cb and total:
                            progress_cb(received / total)
        except requests.RequestException as exc:
            raise APIError(f"Network error during download: {exc}") from exc
