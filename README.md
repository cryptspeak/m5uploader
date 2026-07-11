<h1 align="center">m5uploader</h1>

<p align="center">
  <b>A minimal, security-conscious replacement for M5Burner's account and firmware features</b>
</p>

<p align="center">
  <a href="#"><img src="https://img.shields.io/badge/status-alpha-yellow.svg"></a>
  <a href="#"><img src="https://img.shields.io/badge/platform-Linux%20%7C%20Windows%20%7C%20macOS-success.svg"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-Apache--2.0-blue.svg"></a>
  <a href="../../releases/latest"><img src="https://img.shields.io/github/v/release/cryptspeak/m5uploader?label=download&color=brightgreen"></a>
</p>

---

## Screenshot

<p align="center">
  <img src="docs/screenshot.png" alt="m5uploader screenshot" width="800">
</p>

## Background

[M5Burner](https://docs.m5stack.com/en/quick_start/m5burner/intro) is M5Stack's official desktop app for browsing, downloading, and publishing device firmware. While using it, I found a number of security issues in the underlying Electron app - I've written up the full findings here: **[malloc.pw/2026/07/11/m5burner-is-a-mess](https://malloc.pw/2026/07/11/m5burner-is-a-mess/)**.

m5uploader is a from-scratch, from-first-principles reimplementation of the account and firmware-catalog side of M5Burner, built with a much smaller attack surface. It is **not** a fork or a patched version of the official app.

## Features

- **HTTPS-only.** Every request goes to the real M5Stack API over HTTPS with TLS verification on (the default `requests` behavior - never disabled). No plaintext HTTP, ever.
- **No embedded browser.** The GUI is native [ttkbootstrap](https://ttkbootstrap.readthedocs.io/) (themed `ttk`) - no Chromium, no Node, no remote page ever loaded into the app. Same look on Linux, Windows, and macOS, with a light/dark toggle.
- **Password isn't stored.** Only the session token is persisted, in a plain `0600`-permissioned file rather than an OS keychain, which on Linux depends on a Secret Service backend that isn't always present or unlocked.
- **Honest login state.** The saved token is checked on startup against an endpoint that actually requires auth, not just the public catalog. If it's no longer valid, you're asked to log in again instead of the UI pretending you're still logged in.
- **No automatic telemetry.** `ping_firmware_download()` exists for the same analytics endpoint the official app pings on every download, but the GUI never calls it.
- **No OS-specific shell-outs.** Same codebase, same behavior on Linux, Windows, and macOS.

### Account

Log in with your M5Stack account email/password; the session token is persisted so you don't need to log in every run. Registration is out of scope, same as the official app - register normally in your browser, then log in here.

### Browse Firmware

The full public firmware catalog, searchable and filterable by device, with cover art, descriptions, and per-version downloads. Redeem a share code someone sent you from a small dialog in the toolbar.

### My Firmware

Everything published under your account: publish a new firmware or add a version to an existing one, edit metadata/files, toggle public/private visibility, delete a version, and create/rotate share codes.

### Planned

**On-device flashing** is planned but not yet implemented - it's being held back until the account/firmware API integration above is fully solid. Currently out of scope by design: an in-app device manager, and in-app account registration.

## Running from source

Requires Python 3.10+.

```sh
git clone https://github.com/cryptspeak/m5uploader.git
cd m5uploader
```

**Linux / macOS:**

```sh
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python -m m5uploader
```

**Windows (PowerShell):**

```powershell
python -m venv venv
venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m m5uploader
```

**Windows (cmd.exe):**

```bat
python -m venv venv
venv\Scripts\activate.bat
pip install -r requirements.txt
python -m m5uploader
```

## Building a standalone executable

A standalone executable can be built with [PyInstaller](https://pyinstaller.org/):

**Linux / Windows** (single-file executable):

```sh
pip install pyinstaller
pyinstaller --onefile --windowed --name m5uploader run.py
```

**macOS** (`.app` bundle - PyInstaller deprecates `--onefile` combined with `--windowed` on macOS):

```sh
pip install pyinstaller
pyinstaller --onedir --windowed --name m5uploader run.py
```

The result is written to `dist/`. CI builds this automatically on every push (see [Build](.github/workflows/build.yml)) and publishes it to the [Releases](../../releases) page whenever a `v*` tag is pushed.

## Project layout

```
m5uploader/
  m5uploader/
    api.py           M5Stack API client (login, catalog, share codes,
                      publish/edit/delete/visibility, cover compression)
    auth_store.py    Plain 0600-permissioned token storage (token + username)
    config.py        Paths / hosts
    gui.py           ttkbootstrap GUI (Account / Browse Firmware / My Firmware)
  run.py             PyInstaller entry point
  requirements.txt
```

## Security

Found a security issue in m5uploader itself? Please open an issue.

## License

[Apache License 2.0](LICENSE).
