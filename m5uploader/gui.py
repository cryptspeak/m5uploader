"""ttkbootstrap GUI for m5uploader.

Self-contained: talks to the M5Stack HTTPS API directly and never loads
any m5stack.com web page (the official app renders its entire UI from a
remote page loaded into a nodeIntegration-enabled BrowserWindow - see
draft.md in the m5burner folder for why that's the headline finding
this project exists to route around).

Scope: account (login/register) + browse the public firmware catalog +
download/share firmware. No device manager, no on-device flashing.
"""

import io
import os
import queue
import re
import sys
import threading
import tkinter as tk
import webbrowser
from tkinter import filedialog

import ttkbootstrap as tb
from PIL import Image, ImageTk
from ttkbootstrap.constants import BOTH, END, LEFT, RIGHT, X, Y, W
from ttkbootstrap.dialogs import Messagebox

from . import (
    __version__, auth_store, catalog_cache, config, firmware_cache, flashing, image_cache, update_check,
)
from .api import APIError, M5StackAPI

LIGHT_THEME = "flatly"
DARK_THEME = "darkly"

COLUMNS = ("name", "category", "author", "downloads", "latest")
COLUMN_LABELS = {
    "name": "Name",
    "category": "Device",
    "author": "Author",
    "downloads": "Downloads",
    "latest": "Latest version",
}

# Confirmed from the official app's own device dropdown (`categoryList` in
# packages/view/main.*.js) - the exact display labels for the `category`
# values it knows about. It's an old build (2023) so newer devices (e.g.
# "cardputer", "tab5", "dinmeter") aren't in it; those fall back to a plain
# title-case of the raw value below rather than guessing a label.
KNOWN_DEVICE_LABELS = {
    "core": "Core",
    "core2 & tough": "Core2 & Tough",
    "cores3": "CoreS3",
    "stickc": "StickC",
    "stickv & unitv": "StickV & UnitV",
    "t-lite": "T-Lite",
    "atom": "Atom",
    "atoms3": "AtomS3",
    "timercam": "TimerCam",
    "paper": "Paper",
    "coreink": "CoreInk",
    "stamp": "Stamp",
    "stamps3": "StampS3",
    "station": "Station",
}


def device_label(value: str) -> str:
    if not value:
        return ""
    return KNOWN_DEVICE_LABELS.get(value, value.title())


_NUM_RE = re.compile(r"(\d+)")


def _natural_key(value):
    """Sort key that orders embedded numbers numerically (v2.1.10 after v2.1.9)."""
    return [int(part) if part.isdigit() else part.lower() for part in _NUM_RE.split(str(value))]


def _format_age(seconds: float) -> str:
    minutes = int(seconds // 60)
    return "just now" if minutes < 1 else f"{minutes}m ago"


def _latest_version(fw: dict):
    versions = [v for v in fw.get("versions", []) if v.get("published")]
    return versions[-1] if versions else None


def _sort_value(fw: dict, col: str):
    if col == "downloads":
        return fw.get("download", 0) or 0
    if col == "latest":
        latest = _latest_version(fw)
        return _natural_key(latest["version"]) if latest else []
    return _natural_key(fw.get(col, ""))


def _extract_fid_and_file(resp_json):
    """Pull the new firmware's fid + version file out of the publish
    response. Confirmed 2026-07-08 from a real capture of the official
    app's own `POST /api/admin/firmware` response: it's the firmware
    object directly (no `data` wrapper), e.g. `{"fid": "...", ...,
    "versions": [{"file": "...", "version": "...", ...}]}` - the
    `.get("data", resp_json)` fallback below exists for robustness
    against a differently-shaped response, not because one was
    expected."""
    doc = resp_json.get("data", resp_json) if isinstance(resp_json, dict) else None
    if not isinstance(doc, dict):
        return None, None
    fid = doc.get("fid")
    versions = doc.get("versions") or []
    file_ = versions[-1].get("file") if versions else None
    return fid, file_


class ReleaseDialog(tb.Toplevel):
    """New Release window, opened blank from My Firmware. Publishes a
    brand new firmware. To add a version to one of yours instead, type
    its name exactly - the server tells the two cases apart by whether
    `name` matches one of yours exactly, there's no separate "add a
    version" endpoint, and no client-side prefill for it either."""

    def __init__(self, app: "App"):
        super().__init__(
            title="Publish new firmware", size=(560, 680), minsize=(480, 560),
            transient=app, resizable=(True, True),
        )
        self.app = app
        self.queue = queue.Queue()

        self.path_var = tk.StringVar()
        self.name_var = tk.StringVar()
        self.version_var = tk.StringVar()
        self.category_var = tk.StringVar()
        self.github_var = tk.StringVar()
        self.cover_var = tk.StringVar()
        self.public_var = tk.BooleanVar(value=True)

        body = tb.Frame(self, padding=16)
        body.pack(fill=BOTH, expand=True)

        row1 = tb.Frame(body)
        row1.pack(fill=X, pady=6)
        tb.Label(row1, text="Firmware (.bin)", width=14, anchor=W).pack(side=LEFT)
        tb.Entry(row1, textvariable=self.path_var).pack(side=LEFT, padx=8, fill=X, expand=True)
        tb.Button(row1, text="Browse...", bootstyle="secondary-outline", command=self._browse_firmware).pack(side=LEFT)

        row2 = tb.Frame(body)
        row2.pack(fill=X, pady=6)
        tb.Label(row2, text="Name", width=14, anchor=W).pack(side=LEFT)
        tb.Entry(row2, textvariable=self.name_var).pack(side=LEFT, padx=8, fill=X, expand=True)

        row3 = tb.Frame(body)
        row3.pack(fill=X, pady=6)
        tb.Label(row3, text="Version", width=14, anchor=W).pack(side=LEFT)
        tb.Entry(row3, textvariable=self.version_var, width=16).pack(side=LEFT, padx=8)
        tb.Label(row3, text="Device").pack(side=LEFT, padx=(16, 0))
        tb.Combobox(row3, textvariable=self.category_var, width=18, values=app._known_categories).pack(
            side=LEFT, padx=8
        )

        row4 = tb.Frame(body)
        row4.pack(fill=X, pady=6)
        tb.Label(row4, text="Cover image", width=14, anchor=W).pack(side=LEFT)
        tb.Entry(row4, textvariable=self.cover_var).pack(side=LEFT, padx=8, fill=X, expand=True)
        tb.Button(row4, text="Browse...", bootstyle="secondary-outline", command=self._browse_cover).pack(side=LEFT)

        row5 = tb.Frame(body)
        row5.pack(fill=X, pady=6)
        tb.Label(row5, text="GitHub (optional)", width=14, anchor=W).pack(side=LEFT)
        tb.Entry(row5, textvariable=self.github_var).pack(side=LEFT, padx=8, fill=X, expand=True)

        tb.Label(body, text="Description").pack(anchor=W, pady=(6, 2))
        self.desc_text = tk.Text(body, height=6, wrap="word")
        self.desc_text.pack(fill=BOTH, expand=True)

        tb.Checkbutton(
            body, text="Make public immediately",
            variable=self.public_var, bootstyle="round-toggle",
        ).pack(anchor=W, pady=(10, 0))

        action_row = tb.Frame(body)
        action_row.pack(fill=X, pady=(12, 0))
        self.upload_btn = tb.Button(action_row, text="Upload", bootstyle="success", command=self._on_upload)
        self.upload_btn.pack(side=LEFT)
        self.progress = tb.Progressbar(action_row, mode="indeterminate", bootstyle="success")

        self.status_label = tb.Label(body, text="", bootstyle="secondary")
        self.status_label.pack(anchor=W, pady=(10, 0))

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(150, self._poll)

    def _browse_firmware(self):
        path = filedialog.askopenfilename(
            title="Select firmware .bin", filetypes=[("Firmware binary", "*.bin"), ("All files", "*.*")],
            parent=self,
        )
        if path:
            self.path_var.set(path)

    def _browse_cover(self):
        path = filedialog.askopenfilename(
            title="Select cover image",
            filetypes=[("Images", "*.png *.jpg *.jpeg *.gif *.webp"), ("All files", "*.*")],
            parent=self,
        )
        if path:
            self.cover_var.set(path)

    def _on_upload(self):
        file_path = self.path_var.get().strip()
        name = self.name_var.get().strip()
        version = self.version_var.get().strip()
        if not file_path or not name or not version:
            Messagebox.show_warning("Firmware file, name, and version are required.", "m5uploader")
            return

        category = self.category_var.get().strip()
        github = self.github_var.get().strip()
        cover_path = self.cover_var.get().strip() or None
        description = self.desc_text.get("1.0", END).strip()
        make_public = self.public_var.get()

        self.upload_btn.config(state="disabled", text="Uploading...")
        self.progress.pack(side=LEFT, padx=(10, 0), fill=X, expand=True)
        self.progress.start(12)
        self.status_label.config(text="", bootstyle="secondary")

        def worker():
            try:
                result = self.app.api.publish_firmware(
                    file_path, name,
                    description=description, category=category,
                    version=version, github=github, cover_path=cover_path,
                )
                note = ""
                if make_public:
                    fid, version_file = _extract_fid_and_file(result)
                    if fid and version_file:
                        try:
                            self.app.api.set_publish_state(fid, version_file, True)
                        except APIError:
                            note = " (couldn't confirm it was made public - check the list after refreshing.)"
                    else:
                        note = " (couldn't confirm it was made public - check the list after refreshing.)"
                self.queue.put(("ok", note))
            except APIError as exc:
                self.queue.put(("err", str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def _poll(self):
        try:
            while True:
                kind, payload = self.queue.get_nowait()
                if kind == "ok":
                    self.app._load_own_firmware()
                    Messagebox.show_info(f"Firmware uploaded.{payload}", "m5uploader")
                    self._on_close()
                    return
                elif kind == "err":
                    self.progress.stop()
                    self.progress.pack_forget()
                    self.upload_btn.config(state="normal", text="Upload")
                    self.status_label.config(text=payload, bootstyle="danger")
        except queue.Empty:
            pass
        if self.winfo_exists():
            self.after(150, self._poll)

    def _on_close(self):
        if self.app._release_dialog is self:
            self.app._release_dialog = None
        self.destroy()


class ChangeDialog(tb.Toplevel):
    """Edit an existing version's metadata (optionally replacing its
    firmware binary/cover), and manage its share code, from one window."""

    def __init__(self, app: "App", fw: dict, version: dict):
        super().__init__(
            title=f"Change {fw.get('name')} {version.get('version')}",
            size=(540, 640), minsize=(480, 560), transient=app, resizable=(True, True),
        )
        self.app = app
        self.fw = fw
        self.version = version
        self.queue = queue.Queue()

        self.name_var = tk.StringVar(value=fw.get("name", ""))
        self.version_var = tk.StringVar(value=version.get("version", ""))
        self.category_var = tk.StringVar(value=fw.get("category", ""))
        self.github_var = tk.StringVar(value=fw.get("github", ""))
        self.firmware_var = tk.StringVar()
        self.cover_var = tk.StringVar()
        self.share_code_var = tk.StringVar()

        body = tb.Frame(self, padding=16)
        body.pack(fill=BOTH, expand=True)

        row = tb.Frame(body)
        row.pack(fill=X, pady=4)
        tb.Label(row, text="Name", width=12, anchor=W).pack(side=LEFT)
        tb.Entry(row, textvariable=self.name_var).pack(side=LEFT, padx=6, fill=X, expand=True)

        row = tb.Frame(body)
        row.pack(fill=X, pady=4)
        tb.Label(row, text="Version", width=12, anchor=W).pack(side=LEFT)
        tb.Entry(row, textvariable=self.version_var, width=14).pack(side=LEFT, padx=6)
        tb.Label(row, text="Device").pack(side=LEFT, padx=(8, 0))
        tb.Combobox(row, textvariable=self.category_var, width=16, values=app._known_categories).pack(
            side=LEFT, padx=6
        )

        row = tb.Frame(body)
        row.pack(fill=X, pady=4)
        tb.Label(row, text="GitHub", width=12, anchor=W).pack(side=LEFT)
        tb.Entry(row, textvariable=self.github_var).pack(side=LEFT, padx=6, fill=X, expand=True)

        tb.Label(body, text="Description").pack(anchor=W, pady=(6, 2))
        self.desc_text = tk.Text(body, height=5, wrap="word")
        self.desc_text.pack(fill=X, pady=(0, 6))
        self.desc_text.insert("1.0", fw.get("description", ""))

        row = tb.Frame(body)
        row.pack(fill=X, pady=4)
        tb.Label(row, text="New firmware", width=12, anchor=W).pack(side=LEFT)
        tb.Entry(row, textvariable=self.firmware_var).pack(side=LEFT, padx=6, fill=X, expand=True)
        tb.Button(row, text="Browse...", bootstyle="secondary-outline", command=self._browse_firmware).pack(side=LEFT)

        row = tb.Frame(body)
        row.pack(fill=X, pady=4)
        tb.Label(row, text="New cover", width=12, anchor=W).pack(side=LEFT)
        tb.Entry(row, textvariable=self.cover_var).pack(side=LEFT, padx=6, fill=X, expand=True)
        tb.Button(row, text="Browse...", bootstyle="secondary-outline", command=self._browse_cover).pack(side=LEFT)

        share_frame = tb.Labelframe(body, text="Share code", padding=10)
        share_frame.pack(fill=X, pady=(12, 0))
        tb.Entry(share_frame, textvariable=self.share_code_var, width=20).pack(side=LEFT, padx=(0, 6))
        tb.Button(
            share_frame, text="Get code", bootstyle="secondary-outline", command=self._on_get_share_code
        ).pack(side=LEFT, padx=(0, 4))
        tb.Button(
            share_frame, text="Revoke", bootstyle="danger-outline",
            command=self._on_revoke_share_code,
        ).pack(side=LEFT)

        action_row = tb.Frame(body)
        action_row.pack(fill=X, pady=(14, 0))
        self.save_btn = tb.Button(action_row, text="Save changes", bootstyle="primary", command=self._on_save)
        self.save_btn.pack(side=LEFT)

        self.status_label = tb.Label(body, text="", bootstyle="secondary")
        self.status_label.pack(anchor=W, pady=(10, 0))

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(150, self._poll)

    def _browse_firmware(self):
        path = filedialog.askopenfilename(
            title="Select firmware .bin", filetypes=[("Firmware binary", "*.bin"), ("All files", "*.*")],
            parent=self,
        )
        if path:
            self.firmware_var.set(path)

    def _browse_cover(self):
        path = filedialog.askopenfilename(
            title="Select cover image",
            filetypes=[("Images", "*.png *.jpg *.jpeg *.gif *.webp"), ("All files", "*.*")],
            parent=self,
        )
        if path:
            self.cover_var.set(path)

    def _on_save(self):
        fid = self.fw["fid"]
        version_file = self.version["file"]
        name = self.name_var.get().strip()
        category = self.category_var.get().strip()
        github = self.github_var.get().strip()
        new_version = self.version_var.get().strip()
        description = self.desc_text.get("1.0", END).strip()
        file_path = self.firmware_var.get().strip() or None
        cover_path = self.cover_var.get().strip() or None

        self.save_btn.config(state="disabled", text="Saving...")
        self.status_label.config(text="", bootstyle="secondary")

        def worker():
            try:
                self.app.api.update_firmware(
                    fid, version_file, name,
                    description=description, category=category,
                    version=new_version, github=github,
                    file_path=file_path, cover_path=cover_path,
                )
                self.queue.put(("save_ok", None))
            except APIError as exc:
                self.queue.put(("save_err", str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def _on_get_share_code(self):
        fid = self.fw["fid"]
        version_file = self.version["file"]

        def worker():
            try:
                code = self.app.api.create_share_code(fid, version_file)
                self.queue.put(("share_ok", code))
            except APIError as exc:
                self.queue.put(("share_err", str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def _on_revoke_share_code(self):
        code = self.share_code_var.get().strip()
        if not code:
            Messagebox.show_warning("No share code to revoke.", "m5uploader")
            return
        if Messagebox.yesno(
            f"Revoke share code '{code}'? Anyone using it will lose access.\n\n"
            "Note: this API doesn't fully disable sharing - it invalidates this "
            "code and immediately issues a new one, which will be shown here "
            "and is itself live and shareable.",
            "m5uploader", localize=False,
        ) != "Yes":
            return

        def worker():
            try:
                new_code = self.app.api.revoke_share_code(code)
                self.queue.put(("revoke_ok", new_code))
            except APIError as exc:
                self.queue.put(("revoke_err", str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def _poll(self):
        try:
            while True:
                kind, payload = self.queue.get_nowait()
                if kind == "save_ok":
                    self.save_btn.config(state="normal", text="Save changes")
                    self.status_label.config(text="Saved.", bootstyle="success")
                    self.app._load_own_firmware()
                elif kind == "save_err":
                    self.save_btn.config(state="normal", text="Save changes")
                    self.status_label.config(text=payload, bootstyle="danger")
                elif kind == "share_ok":
                    self.share_code_var.set(payload)
                    self.status_label.config(text="Share code created.", bootstyle="success")
                elif kind == "share_err":
                    self.status_label.config(text=payload, bootstyle="danger")
                elif kind == "revoke_ok":
                    new_code = payload
                    self.share_code_var.set(new_code)
                    self.status_label.config(
                        text="Old code revoked - a new code was issued (shown above) and is live.",
                        bootstyle="success",
                    )
                elif kind == "revoke_err":
                    self.status_label.config(text=payload, bootstyle="danger")
        except queue.Empty:
            pass
        if self.winfo_exists():
            self.after(150, self._poll)

    def _on_close(self):
        if self.app._change_dialog is self:
            self.app._change_dialog = None
        self.destroy()


class ShareCodeDialog(tb.Toplevel):
    """Redeem a firmware someone shared with you by its share code."""

    def __init__(self, app: "App"):
        super().__init__(
            title="Redeem share code", size=(420, 200), minsize=(380, 180),
            transient=app, resizable=(True, False),
        )
        self.app = app
        self.queue = queue.Queue()
        self.code_var = tk.StringVar()
        self.progress_var = tk.DoubleVar(value=0)

        body = tb.Frame(self, padding=16)
        body.pack(fill=BOTH, expand=True)

        row = tb.Frame(body)
        row.pack(fill=X, pady=(0, 12))
        tb.Label(row, text="Share code").pack(side=LEFT)
        entry = tb.Entry(row, textvariable=self.code_var, width=24)
        entry.pack(side=LEFT, padx=8)
        entry.focus_set()

        action_row = tb.Frame(body)
        action_row.pack(fill=X)
        self.fetch_btn = tb.Button(action_row, text="Fetch", bootstyle="primary", command=self._on_fetch)
        self.fetch_btn.pack(side=LEFT)
        self.progress = tb.Progressbar(action_row, variable=self.progress_var, maximum=100, bootstyle="success")

        self.status_label = tb.Label(body, text="", bootstyle="secondary")
        self.status_label.pack(anchor=W, pady=(12, 0))

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(150, self._poll)

    def _on_fetch(self):
        code = self.code_var.get().strip()
        if not code:
            return

        self.fetch_btn.config(state="disabled", text="Resolving...")
        self.status_label.config(text="", bootstyle="secondary")

        def worker():
            try:
                filename = self.app.api.resolve_share_code(code)
                self.queue.put(("resolved", filename))
            except APIError as exc:
                self.queue.put(("err", str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def _start_download(self, filename, dest):
        self.fetch_btn.config(text="Downloading...")
        self.progress.pack(side=LEFT, padx=(10, 0), fill=X, expand=True)
        self.progress_var.set(0)

        def worker():
            try:
                def progress(frac):
                    self.queue.put(("progress", frac * 100))

                self.app.api.download_share_firmware(filename, dest, progress)
                self.queue.put(("ok", dest))
            except APIError as exc:
                self.queue.put(("err", str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def _poll(self):
        try:
            while True:
                kind, payload = self.queue.get_nowait()
                if kind == "resolved":
                    filename = payload
                    suggested = filename if filename.endswith(".bin") else f"{filename}.bin"
                    dest = filedialog.asksaveasfilename(
                        initialfile=suggested, defaultextension=".bin", parent=self
                    )
                    if not dest:
                        self.fetch_btn.config(state="normal", text="Fetch")
                        continue
                    self._start_download(filename, dest)
                elif kind == "progress":
                    self.progress_var.set(payload)
                elif kind == "ok":
                    Messagebox.show_info(f"Downloaded to {payload}", "m5uploader")
                    self._on_close()
                    return
                elif kind == "err":
                    self.fetch_btn.config(state="normal", text="Fetch")
                    self.progress.pack_forget()
                    self.status_label.config(text=payload, bootstyle="danger")
        except queue.Empty:
            pass
        if self.winfo_exists():
            self.after(150, self._poll)

    def _on_close(self):
        if self.app._share_dialog is self:
            self.app._share_dialog = None
        self.destroy()


class App(tb.Window):
    def __init__(self):
        super().__init__(title="m5uploader", themename=LIGHT_THEME, minsize=(900, 620))
        self._size_to_screen()

        config.ensure_dirs()

        self.api = M5StackAPI()
        self.msg_queue = queue.Queue()
        self.dark_mode = False

        self.username_var = tk.StringVar(value="Not logged in")
        self.search_var = tk.StringVar()
        self.category_var = tk.StringVar(value="All devices")
        self.version_var = tk.StringVar()
        self.progress_var = tk.DoubleVar(value=0)

        self.flash_port_var = tk.StringVar()
        self.flash_file_var = tk.StringVar()
        self.flash_erase_var = tk.BooleanVar(value=False)
        self.flash_progress_var = tk.DoubleVar(value=0)
        self._flash_ports_by_label = {}
        self._flash_cancel_event = None
        self._update_url = None

        self._catalog = []
        self._filtered_fids = []
        self._fw_by_fid = {}
        self._cover_cache = {}
        self._selected_fid = None
        self._search_after_id = None
        self._sort_col = "downloads"
        self._sort_reverse = True
        self._category_value_by_label = {}

        self._own_firmware = []
        self._own_row_by_iid = {}
        self._selected_own_iid = None
        self._known_categories = []
        self._release_dialog = None
        self._change_dialog = None
        self._share_dialog = None

        self._build_layout()
        self._try_restore_session()
        self.after(150, self._poll_queue)
        self.after(2000, self._check_for_update)

    # ------------------------------------------------------------------
    # top-level layout
    # ------------------------------------------------------------------

    def _size_to_screen(self):
        """Default to a window sized relative to the display instead of a
        fixed pixel size, so it isn't cramped on large monitors or
        oversized on small/laptop ones. Clamped to a sane range either
        way; still freely resizable afterwards."""
        screen_w = self.winfo_screenwidth()
        screen_h = self.winfo_screenheight()
        width = min(max(int(screen_w * 0.7), 1100), 1700)
        height = min(max(int(screen_h * 0.75), 700), 1050)
        x = (screen_w - width) // 2
        y = (screen_h - height) // 2
        self.geometry(f"{width}x{height}+{x}+{y}")

    def _build_layout(self):
        top = tb.Frame(self, padding=10)
        top.pack(fill=X)
        title_row = tb.Frame(top)
        title_row.pack(side=LEFT)
        tb.Label(title_row, text="m5uploader", font=("", 14, "bold")).pack(side=LEFT)
        tb.Label(title_row, text=f"v{__version__}", bootstyle="secondary", font=("", 9)).pack(
            side=LEFT, padx=(6, 0), anchor="s", pady=(0, 2)
        )
        tb.Label(top, textvariable=self.username_var, bootstyle="secondary").pack(side=LEFT, padx=16)
        tb.Button(top, text="Toggle dark mode", bootstyle="link", command=self._toggle_theme).pack(side=RIGHT)

        # Hidden until _check_for_update() finds a newer release. Purely
        # informational - clicking it just opens the GitHub release page
        # in the user's own browser (see _on_update_click); nothing is
        # ever downloaded or run automatically.
        self.update_banner = tb.Frame(top)
        self.update_label = tb.Label(self.update_banner, text="", bootstyle="warning", cursor="hand2")
        self.update_label.pack(side=LEFT)
        self.update_label.bind("<Button-1>", self._on_update_click)
        tb.Button(
            self.update_banner, text="×", bootstyle="link", width=2, command=self.update_banner.pack_forget
        ).pack(side=LEFT)

        notebook = tb.Notebook(self)
        notebook.pack(fill=BOTH, expand=True, padx=10, pady=10)
        self.notebook = notebook

        self.account_tab = tb.Frame(notebook, padding=16)
        self.browse_tab = tb.Frame(notebook, padding=16)
        self.mine_tab = tb.Frame(notebook, padding=16)
        self.flash_tab = tb.Frame(notebook, padding=16)

        notebook.add(self.account_tab, text="Account")
        notebook.add(self.browse_tab, text="Browse Firmware")
        notebook.add(self.mine_tab, text="My Firmware")
        notebook.add(self.flash_tab, text="Flash Firmware")

        self._build_account_tab()
        self._build_browse_tab()
        self._build_mine_tab()
        self._build_flash_tab()

        notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)

    def _toggle_theme(self):
        self.dark_mode = not self.dark_mode
        self.style.theme_use(DARK_THEME if self.dark_mode else LIGHT_THEME)

    def _on_tab_changed(self, event):
        tab_text = event.widget.tab(event.widget.select(), "text")
        if tab_text == "Browse Firmware" and not self._catalog:
            self._load_catalog()
        elif tab_text == "My Firmware" and self.api.token:
            self._load_own_firmware()

    # ------------------------------------------------------------------
    # account tab
    # ------------------------------------------------------------------

    def _build_account_tab(self):
        outer = self.account_tab

        # -- logged-out: login form --
        self.login_form_frame = tb.Frame(outer)

        form = tb.Labelframe(self.login_form_frame, text="Log in to your M5Stack account", padding=16)
        form.pack(fill=X, pady=8)

        tb.Label(form, text="Email").grid(row=0, column=0, sticky=W, pady=6)
        self.email_entry = tb.Entry(form, width=38)
        self.email_entry.grid(row=0, column=1, pady=6, padx=8)

        tb.Label(form, text="Password").grid(row=1, column=0, sticky=W, pady=6)
        self.password_entry = tb.Entry(form, width=38, show="*")
        self.password_entry.grid(row=1, column=1, pady=6, padx=8)

        tb.Button(form, text="Log in", bootstyle="primary", command=self._on_login).grid(
            row=2, column=0, columnspan=2, pady=(12, 0), sticky=W
        )

        self.login_status = tb.Label(self.login_form_frame, text="", bootstyle="danger")
        self.login_status.pack(anchor=W, pady=(12, 0))

        tb.Label(
            self.login_form_frame,
            text="Password is never stored, only the session token, locally.",
            bootstyle="secondary",
            justify=LEFT,
        ).pack(anchor=W, pady=(16, 0))

        register_link = tb.Label(
            self.login_form_frame, text="Don't have an account? Register",
            bootstyle="info", cursor="hand2",
        )
        register_link.pack(anchor=W, pady=(8, 0))
        register_link.bind("<Button-1>", self._on_register_click)

        # -- logged-in: account summary + log out --
        self.logged_in_frame = tb.Frame(outer)
        tb.Label(self.logged_in_frame, textvariable=self.username_var, font=("", 13, "bold")).pack(
            anchor=W, pady=(24, 12)
        )
        tb.Button(
            self.logged_in_frame, text="Log out", bootstyle="danger-outline", command=self._on_logout
        ).pack(anchor=W)

        self._refresh_account_view()

    def _refresh_account_view(self):
        if self.api.token:
            self.login_form_frame.pack_forget()
            self.logged_in_frame.pack(fill=BOTH, expand=True)
        else:
            self.logged_in_frame.pack_forget()
            self.login_form_frame.pack(fill=BOTH, expand=True)

    def _try_restore_session(self):
        token, email, username = auth_store.load_session()
        if not token:
            return
        self.api.set_token(token)
        self.api.username = username

        def worker():
            ok = self.api.validate_session()
            self.msg_queue.put(("session_restored", ok, email, username))

        threading.Thread(target=worker, daemon=True).start()

    def _on_login(self):
        email = self.email_entry.get().strip()
        password = self.password_entry.get()
        if not email or not password:
            self.login_status.config(text="Enter email and password.")
            return
        self.login_status.config(text="Logging in...", bootstyle="secondary")

        def worker():
            try:
                result = self.api.login(email, password)
                auth_store.save_session(result["token"], email, result.get("username") or "")
                self.msg_queue.put(("login_ok", result.get("username") or email))
            except APIError as exc:
                self.msg_queue.put(("login_err", str(exc)))

        threading.Thread(target=worker, daemon=True).start()
        self.password_entry.delete(0, END)

    def _on_logout(self):
        self.api.logout()
        auth_store.clear_session()
        self.username_var.set("Not logged in")
        self.login_status.config(text="Logged out.", bootstyle="secondary")
        self._refresh_account_view()

    def _on_register_click(self, event=None):
        if Messagebox.yesno(
            "Registration isn't handled in this app - you'll be taken to "
            "community.m5stack.com/register in your browser. M5Stack then "
            "emails you a verification link; login here won't work until "
            "you've clicked it.\n\nContinue to the registration page?",
            "m5uploader", localize=False,
        ) == "Yes":
            webbrowser.open("https://community.m5stack.com/register")

    def _make_scrollable(self, parent):
        """Wrap parent's content in a vertically scrollable canvas so it's
        still reachable (mouse wheel or scrollbar) if the window is too
        short to show everything at once. Returns the frame to pack
        children into."""
        container = tb.Frame(parent)
        container.pack(fill=BOTH, expand=True)

        canvas = tk.Canvas(container, highlightthickness=0, bd=0)
        scrollbar = tb.Scrollbar(container, orient="vertical", command=canvas.yview, bootstyle="round")
        inner = tb.Frame(canvas, padding=10)
        inner_id = canvas.create_window((0, 0), window=inner, anchor="nw")

        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(inner_id, width=e.width))
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.pack(side=LEFT, fill=BOTH, expand=True)
        scrollbar.pack(side=RIGHT, fill=Y)

        def _wheel(event):
            if event.num == 4:
                canvas.yview_scroll(-1, "units")
            elif event.num == 5:
                canvas.yview_scroll(1, "units")
            else:
                canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        def _bind_wheel(_event):
            canvas.bind_all("<MouseWheel>", _wheel)
            canvas.bind_all("<Button-4>", _wheel)
            canvas.bind_all("<Button-5>", _wheel)

        def _unbind_wheel(_event):
            canvas.unbind_all("<MouseWheel>")
            canvas.unbind_all("<Button-4>")
            canvas.unbind_all("<Button-5>")

        canvas.bind("<Enter>", _bind_wheel)
        canvas.bind("<Leave>", _unbind_wheel)

        return inner

    # ------------------------------------------------------------------
    # browse tab
    # ------------------------------------------------------------------

    def _build_browse_tab(self):
        f = self.browse_tab
        f.columnconfigure(0, weight=3)
        f.columnconfigure(1, weight=2)
        f.rowconfigure(1, weight=1)

        toolbar = tb.Frame(f)
        toolbar.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 8))

        tb.Label(toolbar, text="Search").pack(side=LEFT)
        search_entry = tb.Entry(toolbar, textvariable=self.search_var, width=28)
        search_entry.pack(side=LEFT, padx=(6, 16))
        search_entry.bind("<KeyRelease>", self._on_search_changed)

        tb.Label(toolbar, text="Device").pack(side=LEFT)
        self.category_combo = tb.Combobox(
            toolbar, textvariable=self.category_var, width=22, state="readonly", values=["All devices"]
        )
        self.category_combo.pack(side=LEFT, padx=6)
        self.category_combo.bind("<<ComboboxSelected>>", lambda e: self._apply_filter())

        tb.Button(
            toolbar, text="Refresh catalog", bootstyle="secondary-outline",
            command=lambda: self._load_catalog(force=True),
        ).pack(side=RIGHT)
        tb.Button(
            toolbar, text="Redeem share code...", bootstyle="secondary-outline", command=self._on_open_share_dialog
        ).pack(side=RIGHT, padx=(0, 8))

        self.catalog_status = tb.Label(toolbar, text="", bootstyle="secondary")
        self.catalog_status.pack(side=RIGHT, padx=12)

        # -- list --
        list_frame = tb.Frame(f)
        list_frame.grid(row=1, column=0, sticky="nsew", padx=(0, 12))
        list_frame.rowconfigure(0, weight=1)
        list_frame.columnconfigure(0, weight=1)

        self.tree = tb.Treeview(list_frame, columns=COLUMNS, show="headings", bootstyle="primary")
        for col in COLUMNS:
            self.tree.heading(col, text=COLUMN_LABELS[col], command=lambda c=col: self._sort_by(c))
            self.tree.column(col, width=120 if col != "name" else 220, anchor=W)
        self.tree.grid(row=0, column=0, sticky="nsew")

        vsb = tb.Scrollbar(list_frame, orient="vertical", command=self.tree.yview, bootstyle="round")
        vsb.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.bind("<<TreeviewSelect>>", self._on_select_firmware)

        # -- detail panel (scrollable so nothing gets clipped when the window is short) --
        detail_outer = tb.Labelframe(f, text="Details", padding=4)
        detail_outer.grid(row=1, column=1, sticky="nsew")
        detail = self._make_scrollable(detail_outer)

        self.cover_label = tb.Label(detail)
        self.cover_label.pack(anchor=W, pady=(0, 10))

        self.detail_name = tb.Label(detail, text="Select a firmware", font=("", 12, "bold"), wraplength=320)
        self.detail_name.pack(anchor=W)
        self.detail_meta = tb.Label(detail, text="", bootstyle="secondary", wraplength=320)
        self.detail_meta.pack(anchor=W, pady=(2, 10))

        self.detail_desc = tk.Text(detail, height=6, width=36, wrap="word", borderwidth=0)
        self.detail_desc.configure(state="disabled")
        self.detail_desc.pack(fill=X, pady=(0, 10))

        version_row = tb.Frame(detail)
        version_row.pack(fill=X, pady=(0, 10))
        tb.Label(version_row, text="Version").pack(side=LEFT)
        self.version_combo = tb.Combobox(version_row, textvariable=self.version_var, width=18, state="readonly")
        self.version_combo.pack(side=LEFT, padx=8)

        download_row = tb.Frame(detail)
        download_row.pack(anchor=W)
        self.download_btn = tb.Button(
            download_row, text="Download...", bootstyle="success", command=self._on_download_firmware,
            state="disabled",
        )
        self.download_btn.pack(side=LEFT)
        self.flash_from_catalog_btn = tb.Button(
            download_row, text="Flash...", bootstyle="warning", command=self._on_flash_from_catalog,
            state="disabled",
        )
        self.flash_from_catalog_btn.pack(side=LEFT, padx=(8, 0))

        self.browse_progress = tb.Progressbar(detail, variable=self.progress_var, maximum=100, bootstyle="success")
        self.browse_progress.pack(fill=X, pady=(10, 0))

    def _set_catalog(self, catalog):
        self._catalog = catalog
        self._fw_by_fid = {fw["fid"]: fw for fw in catalog}
        categories = sorted({fw.get("category", "") for fw in catalog if fw.get("category")}, key=device_label)
        self._category_value_by_label = {device_label(c): c for c in categories}
        self.category_combo["values"] = ["All devices", *(device_label(c) for c in categories)]
        self._known_categories = categories
        self._apply_filter()

    def _load_catalog(self, force=False):
        if not force:
            cached = catalog_cache.load_cached_catalog()
            if cached is not None:
                self._set_catalog(cached)
                age = catalog_cache.cache_age()
                if age is not None:
                    self.catalog_status.config(text=f"{self.catalog_status.cget('text')} (cached {_format_age(age)})")
                return

        self.catalog_status.config(text="Loading catalog...")

        def worker():
            try:
                catalog = self.api.list_firmware()
                catalog_cache.save_cached_catalog(catalog)
                self.msg_queue.put(("catalog", catalog))
            except APIError as exc:
                self.msg_queue.put(("catalog_err", str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def _on_search_changed(self, event=None):
        if self._search_after_id:
            self.after_cancel(self._search_after_id)
        self._search_after_id = self.after(200, self._apply_filter)

    def _apply_filter(self):
        query = self.search_var.get().strip().lower()
        label = self.category_var.get()
        category = self._category_value_by_label.get(label) if label != "All devices" else None

        rows = []
        for fw in self._catalog:
            if category is not None and fw.get("category") != category:
                continue
            haystack = f"{fw.get('name', '')} {fw.get('category', '')} {fw.get('author', '')}".lower()
            if query and query not in haystack:
                continue
            rows.append(fw)

        rows.sort(key=lambda fw: _sort_value(fw, self._sort_col), reverse=self._sort_reverse)

        self.tree.delete(*self.tree.get_children())
        self._filtered_fids = []
        for fw in rows:
            latest = _latest_version(fw)
            self.tree.insert(
                "", END, iid=fw["fid"],
                values=(
                    fw.get("name", ""),
                    device_label(fw.get("category", "")),
                    fw.get("author", ""),
                    fw.get("download", 0),
                    latest["version"] if latest else "",
                ),
            )
            self._filtered_fids.append(fw["fid"])

        self.catalog_status.config(text=f"{len(self._filtered_fids)} / {len(self._catalog)} firmwares")
        self._refresh_sort_headers()

    def _sort_by(self, col):
        if self._sort_col == col:
            self._sort_reverse = not self._sort_reverse
        else:
            self._sort_col = col
            self._sort_reverse = False
        self._apply_filter()

    def _refresh_sort_headers(self):
        for col in COLUMNS:
            label = COLUMN_LABELS[col]
            if col == self._sort_col:
                label += " ▼" if self._sort_reverse else " ▲"
            self.tree.heading(col, text=label)

    def _on_select_firmware(self, event=None):
        sel = self.tree.selection()
        if not sel:
            return
        fid = sel[0]
        fw = self._fw_by_fid.get(fid)
        if not fw:
            return
        self._selected_fid = fid

        self.detail_name.config(text=fw.get("name", ""))
        meta_bits = [fw.get("category", ""), fw.get("author", "")]
        if fw.get("download") is not None:
            meta_bits.append(f"{fw['download']} downloads")
        self.detail_meta.config(text=" · ".join(b for b in meta_bits if b))

        self.detail_desc.configure(state="normal")
        self.detail_desc.delete("1.0", END)
        self.detail_desc.insert("1.0", fw.get("description", ""))
        self.detail_desc.configure(state="disabled")

        versions = [v["version"] for v in fw.get("versions", []) if v.get("published")]
        self.version_combo["values"] = versions
        if versions:
            self.version_var.set(versions[-1])
            self.download_btn.config(state="normal")
            self.flash_from_catalog_btn.config(state="normal")
        else:
            self.version_var.set("")
            self.download_btn.config(state="disabled")
            self.flash_from_catalog_btn.config(state="disabled")

        self.cover_label.config(image="", text="")
        cover = fw.get("cover")
        if cover:
            if cover in self._cover_cache:
                photo = self._cover_cache[cover]
                self.cover_label.config(image=photo)
                self.cover_label.image = photo
            else:
                threading.Thread(target=self._fetch_cover, args=(fid, cover, "browse"), daemon=True).start()

    def _fetch_cover(self, key, cover, target):
        cached = image_cache.load_cached_image(cover)
        if cached is not None:
            try:
                image = Image.open(io.BytesIO(cached))
                image.load()
                image.thumbnail((280, 280))
                self.msg_queue.put(("cover", key, cover, image, target))
                return
            except OSError:
                pass  # corrupt cache entry - fall through and re-fetch

        try:
            data = self.api.fetch_cover_image(cover)
            image_cache.save_cached_image(cover, data)
            image = Image.open(io.BytesIO(data))
            image.load()
            image.thumbnail((280, 280))
            self.msg_queue.put(("cover", key, cover, image, target))
        except (APIError, OSError):
            self.msg_queue.put(("cover", key, cover, None, target))

    def _on_download_firmware(self):
        fw = self._fw_by_fid.get(self._selected_fid)
        if not fw:
            return
        version = next((v for v in fw.get("versions", []) if v["version"] == self.version_var.get()), None)
        if not version:
            return
        filename = version["file"]
        suggested = filename if filename.endswith(".bin") else f"{filename}.bin"
        dest = filedialog.asksaveasfilename(initialfile=suggested, defaultextension=".bin")
        if not dest:
            return

        def worker():
            try:
                self.progress_var.set(0)

                def progress(frac):
                    self.msg_queue.put(("progress", frac * 100))

                self.api.download_firmware(filename, dest, progress)
                self.msg_queue.put(("info", f"Downloaded to {dest}"))
            except APIError as exc:
                self.msg_queue.put(("error", str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def _on_flash_from_catalog(self):
        """Download the selected version straight into the local
        firmware cache (skipping the download entirely if it's already
        cached), then switch to the Flash Firmware tab with the file
        pre-filled so the user can pick a port/settings and flash it -
        no separate manual download step needed. The
        ordinary "Download..." button is untouched and still always asks
        where to save."""
        fw = self._fw_by_fid.get(self._selected_fid)
        if not fw:
            return
        version = next((v for v in fw.get("versions", []) if v["version"] == self.version_var.get()), None)
        if not version:
            return
        filename = version["file"]
        dest = firmware_cache.cache_path(filename)

        if firmware_cache.is_cached(filename):
            self.flash_file_var.set(str(dest))
            self.notebook.select(self.flash_tab)
            return

        firmware_cache.ensure_dir()
        self.flash_from_catalog_btn.config(state="disabled", text="Downloading...")

        def worker():
            try:
                self.progress_var.set(0)

                def progress(frac):
                    self.msg_queue.put(("progress", frac * 100))

                self.api.download_firmware(filename, dest, progress)
                firmware_cache.evict_if_needed()
                self.msg_queue.put(("flash_from_catalog_ok", str(dest)))
            except APIError as exc:
                self.msg_queue.put(("flash_from_catalog_err", str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    # ------------------------------------------------------------------
    # share-code redemption (Browse Firmware toolbar)
    # ------------------------------------------------------------------

    def _on_open_share_dialog(self):
        if self._share_dialog is not None and self._share_dialog.winfo_exists():
            self._share_dialog.lift()
            return
        self._share_dialog = ShareCodeDialog(self)

    # ------------------------------------------------------------------
    # my firmwares tab
    # ------------------------------------------------------------------

    def _build_mine_tab(self):
        f = self.mine_tab
        f.columnconfigure(0, weight=3)
        f.columnconfigure(1, weight=2)
        f.rowconfigure(1, weight=1)

        toolbar = tb.Frame(f)
        toolbar.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        tb.Button(toolbar, text="Refresh", bootstyle="secondary-outline", command=self._load_own_firmware).pack(
            side=LEFT
        )
        self.mine_status = tb.Label(toolbar, text="", bootstyle="secondary")
        self.mine_status.pack(side=LEFT, padx=12)
        tb.Button(toolbar, text="New Release...", bootstyle="success", command=self._on_new_release).pack(side=RIGHT)

        list_frame = tb.Frame(f)
        list_frame.grid(row=1, column=0, sticky="nsew", padx=(0, 12))
        list_frame.rowconfigure(0, weight=1)
        list_frame.columnconfigure(0, weight=1)

        mine_columns = ("name", "category", "version", "published", "downloads")
        mine_labels = {
            "name": "Name", "category": "Device", "version": "Version",
            "published": "Public", "downloads": "Downloads",
        }
        self.mine_tree = tb.Treeview(list_frame, columns=mine_columns, show="headings", bootstyle="primary")
        for col in mine_columns:
            self.mine_tree.heading(col, text=mine_labels[col])
            self.mine_tree.column(col, width=110 if col != "name" else 200, anchor=W)
        self.mine_tree.grid(row=0, column=0, sticky="nsew")

        vsb = tb.Scrollbar(list_frame, orient="vertical", command=self.mine_tree.yview, bootstyle="round")
        vsb.grid(row=0, column=1, sticky="ns")
        self.mine_tree.configure(yscrollcommand=vsb.set)
        self.mine_tree.bind("<<TreeviewSelect>>", self._on_select_own_firmware)

        detail_outer = tb.Labelframe(f, text="Selected version", padding=12)
        detail_outer.grid(row=1, column=1, sticky="nsew")

        self.mine_cover_label = tb.Label(detail_outer)
        self.mine_cover_label.pack(anchor=W, pady=(0, 10))

        self.mine_detail_name = tb.Label(
            detail_outer, text="Select a version below", font=("", 12, "bold"), wraplength=280
        )
        self.mine_detail_name.pack(anchor=W)
        self.mine_detail_meta = tb.Label(detail_outer, text="", bootstyle="secondary", wraplength=280, justify=LEFT)
        self.mine_detail_meta.pack(anchor=W, pady=(4, 10))
        self.mine_detail_desc = tk.Text(detail_outer, height=8, wrap="word", borderwidth=0)
        self.mine_detail_desc.configure(state="disabled")
        self.mine_detail_desc.pack(fill=BOTH, expand=True)

        btn_row = tb.Frame(f)
        btn_row.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        self.mine_change_btn = tb.Button(
            btn_row, text="Change...", bootstyle="primary", command=self._on_change_selected, state="disabled"
        )
        self.mine_change_btn.pack(side=LEFT, padx=(0, 8))
        self.mine_visibility_btn = tb.Button(
            btn_row, text="Change Visibility", bootstyle="warning-outline",
            command=self._on_change_visibility, state="disabled",
        )
        self.mine_visibility_btn.pack(side=LEFT, padx=(0, 8))
        self.mine_delete_btn = tb.Button(
            btn_row, text="Delete", bootstyle="danger-outline", command=self._on_delete_selected, state="disabled"
        )
        self.mine_delete_btn.pack(side=LEFT)

        self.mine_edit_status = tb.Label(f, text="", bootstyle="secondary")
        self.mine_edit_status.grid(row=3, column=0, columnspan=2, sticky="w", pady=(8, 0))

    def _load_own_firmware(self):
        if not self.api.token:
            Messagebox.show_warning("Log in first.", "m5uploader")
            return
        self.mine_status.config(text="Loading...")

        def worker():
            try:
                data = self.api.list_own_firmware()
                self.msg_queue.put(("mine", data))
            except APIError as exc:
                self.msg_queue.put(("mine_err", str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def _render_own_firmware(self, data):
        self._own_firmware = data
        self.mine_tree.delete(*self.mine_tree.get_children())
        self._own_row_by_iid = {}
        count = 0
        for fw in data:
            for version in fw.get("versions", []):
                iid = f"{fw['fid']}|||{version.get('version')}"
                self.mine_tree.insert(
                    "", END, iid=iid,
                    values=(
                        fw.get("name", ""),
                        device_label(fw.get("category", "")),
                        version.get("version", ""),
                        "Yes" if version.get("published") else "No",
                        fw.get("download", 0),
                    ),
                )
                self._own_row_by_iid[iid] = (fw, version)
                count += 1
        self.mine_status.config(text=f"{count} version(s) across {len(data)} firmware(s)")
        if self._selected_own_iid not in self._own_row_by_iid:
            self._selected_own_iid = None
            self.mine_detail_name.config(text="Select a version below")
            self.mine_detail_meta.config(text="")
            self.mine_cover_label.config(image="", text="")
            self.mine_detail_desc.configure(state="normal")
            self.mine_detail_desc.delete("1.0", END)
            self.mine_detail_desc.configure(state="disabled")
            for btn in (self.mine_change_btn, self.mine_visibility_btn, self.mine_delete_btn):
                btn.config(state="disabled")

    def _on_select_own_firmware(self, event=None):
        sel = self.mine_tree.selection()
        if not sel:
            return
        fw, version = self._own_row_by_iid.get(sel[0], (None, None))
        if not fw:
            return
        self._selected_own_iid = sel[0]

        self.mine_detail_name.config(text=f"{fw.get('name', '')} - {version.get('version', '')}")
        meta_bits = [
            device_label(fw.get("category", "")),
            "Public" if version.get("published") else "Private draft",
            f"{fw.get('download', 0)} downloads",
        ]
        self.mine_detail_meta.config(text=" · ".join(b for b in meta_bits if b))
        self.mine_detail_desc.configure(state="normal")
        self.mine_detail_desc.delete("1.0", END)
        self.mine_detail_desc.insert("1.0", fw.get("description", ""))
        self.mine_detail_desc.configure(state="disabled")

        self.mine_cover_label.config(image="", text="")
        cover = fw.get("cover")
        if cover:
            if cover in self._cover_cache:
                photo = self._cover_cache[cover]
                self.mine_cover_label.config(image=photo)
                self.mine_cover_label.image = photo
            else:
                threading.Thread(target=self._fetch_cover, args=(sel[0], cover, "mine"), daemon=True).start()

        self.mine_visibility_btn.config(text="Make Private" if version.get("published") else "Make Public")
        for btn in (self.mine_change_btn, self.mine_visibility_btn, self.mine_delete_btn):
            btn.config(state="normal")
        self.mine_edit_status.config(text="", bootstyle="secondary")

    def _on_new_release(self):
        if not self.api.token:
            Messagebox.show_warning("Log in first.", "m5uploader")
            return
        if self._release_dialog is not None and self._release_dialog.winfo_exists():
            self._release_dialog.lift()
            return
        self._release_dialog = ReleaseDialog(self)

    def _on_change_selected(self):
        fw, version = self._own_row_by_iid.get(self._selected_own_iid, (None, None))
        if not fw:
            return
        if self._change_dialog is not None and self._change_dialog.winfo_exists():
            self._change_dialog.lift()
            return
        self._change_dialog = ChangeDialog(self, fw, version)

    def _on_change_visibility(self):
        fw, version = self._own_row_by_iid.get(self._selected_own_iid, (None, None))
        if not fw:
            return
        fid = fw["fid"]
        version_file = version["file"]
        make_public = not version.get("published")
        verb = "public" if make_public else "private"
        if Messagebox.yesno(
            f"Make {fw.get('name')} {version.get('version')} {verb}?", "m5uploader", localize=False
        ) != "Yes":
            return

        self.mine_edit_status.config(text="Updating visibility...", bootstyle="secondary")

        def worker():
            try:
                self.api.set_publish_state(fid, version_file, make_public)
                self.msg_queue.put(("visibility_ok", make_public))
            except APIError as exc:
                self.msg_queue.put(("visibility_err", str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def _on_delete_selected(self):
        fw, version = self._own_row_by_iid.get(self._selected_own_iid, (None, None))
        if not fw:
            return
        if Messagebox.yesno(
            f"Delete version {version.get('version')} of {fw.get('name')}? This can't be undone.",
            "m5uploader", localize=False,
        ) != "Yes":
            return
        fid = fw["fid"]
        version_str = version["version"]

        def worker():
            try:
                self.api.remove_own_firmware(fid, version_str)
                self.msg_queue.put(("delete_ok",))
            except APIError as exc:
                self.msg_queue.put(("delete_err", str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    # ------------------------------------------------------------------
    # queue polling (all cross-thread UI updates funnel through here)
    # ------------------------------------------------------------------

    def _poll_queue(self):
        if not self.winfo_exists():
            return
        try:
            while True:
                kind, *payload = self.msg_queue.get_nowait()
                self._handle_message(kind, payload)
        except queue.Empty:
            pass
        self.after(150, self._poll_queue)

    def _handle_message(self, kind, payload):
        if kind == "session_restored":
            ok, email, username = payload
            self.username_var.set(f"Logged in as {username or email}" if ok else "Not logged in")
            if not ok:
                self.api.logout()
                auth_store.clear_session()
            self._refresh_account_view()
        elif kind == "login_ok":
            username = payload[0]
            self.username_var.set(f"Logged in as {username}")
            self.login_status.config(text="Login successful.", bootstyle="success")
            self._refresh_account_view()
        elif kind == "login_err":
            self.login_status.config(
                text=f"{payload[0]} (if you just registered, make sure you've verified your email first)",
                bootstyle="danger",
            )
        elif kind == "catalog":
            self._set_catalog(payload[0])
        elif kind == "catalog_err":
            self.catalog_status.config(text="Failed to load catalog")
            Messagebox.show_error(payload[0], "m5uploader")
        elif kind == "cover":
            key, cover, image, target = payload
            if image is None:
                return
            photo = ImageTk.PhotoImage(image)
            self._cover_cache[cover] = photo
            if target == "browse" and self._selected_fid == key:
                self.cover_label.config(image=photo)
                self.cover_label.image = photo
            elif target == "mine" and self._selected_own_iid == key:
                self.mine_cover_label.config(image=photo)
                self.mine_cover_label.image = photo
        elif kind == "info":
            Messagebox.show_info(payload[0], "m5uploader")
        elif kind == "error":
            Messagebox.show_error(payload[0], "m5uploader")
        elif kind == "progress":
            self.progress_var.set(payload[0])
        elif kind == "mine":
            self._render_own_firmware(payload[0])
        elif kind == "mine_err":
            self.mine_status.config(text="Failed to load")
            Messagebox.show_error(payload[0], "m5uploader")
        elif kind == "delete_ok":
            self.mine_edit_status.config(text="Deleted.", bootstyle="success")
            self._selected_own_iid = None
            self._load_own_firmware()
        elif kind == "delete_err":
            self.mine_edit_status.config(text=payload[0], bootstyle="danger")
        elif kind == "visibility_ok":
            published = payload[0]
            self.mine_edit_status.config(
                text="Now public." if published else "Now private.", bootstyle="success"
            )
            self._load_own_firmware()
        elif kind == "visibility_err":
            self.mine_edit_status.config(text=payload[0], bootstyle="danger")
        elif kind == "flash_line":
            self.flash_log.configure(state="normal")
            self.flash_log.insert(END, payload[0] + "\n")
            self.flash_log.see(END)
            self.flash_log.configure(state="disabled")
        elif kind == "flash_progress":
            self.flash_progress_var.set(payload[0])
        elif kind == "flash_ok":
            self._flash_cancel_event = None
            self.flash_btn.config(state="normal")
            self.flash_cancel_btn.config(state="disabled")
            Messagebox.show_info("Flashing complete.", "m5uploader")
        elif kind == "flash_cancelled":
            self._flash_cancel_event = None
            self.flash_btn.config(state="normal")
            self.flash_cancel_btn.config(state="disabled")
            self.flash_progress_var.set(0)
        elif kind == "flash_err":
            self._flash_cancel_event = None
            self.flash_btn.config(state="normal")
            self.flash_cancel_btn.config(state="disabled")
            Messagebox.show_error(payload[0], "m5uploader")
        elif kind == "update_available":
            info = payload[0]
            self._update_url = info.url
            self.update_label.config(text=f"{info.tag} is available")
            self.update_banner.pack(side=RIGHT, padx=(0, 12))
        elif kind == "flash_from_catalog_ok":
            self.flash_from_catalog_btn.config(state="normal", text="Flash...")
            self.flash_file_var.set(payload[0])
            self.notebook.select(self.flash_tab)
        elif kind == "flash_from_catalog_err":
            self.flash_from_catalog_btn.config(state="normal", text="Flash...")
            Messagebox.show_error(payload[0], "m5uploader")

    # ------------------------------------------------------------------
    # flash firmware tab
    # ------------------------------------------------------------------

    def _build_flash_tab(self):
        f = self.flash_tab

        port_row = tb.Frame(f)
        port_row.pack(fill=X, pady=6)
        tb.Label(port_row, text="Serial port", width=14, anchor=W).pack(side=LEFT)
        self.flash_port_combo = tb.Combobox(
            port_row, textvariable=self.flash_port_var, width=42, state="readonly"
        )
        self.flash_port_combo.pack(side=LEFT, padx=8)
        tb.Button(
            port_row, text="Refresh ports", bootstyle="secondary-outline", command=self._refresh_serial_ports
        ).pack(side=LEFT)

        file_row = tb.Frame(f)
        file_row.pack(fill=X, pady=6)
        tb.Label(file_row, text="Firmware (.bin)", width=14, anchor=W).pack(side=LEFT)
        tb.Entry(file_row, textvariable=self.flash_file_var).pack(side=LEFT, padx=8, fill=X, expand=True)
        tb.Button(
            file_row, text="Browse...", bootstyle="secondary-outline", command=self._on_browse_flash_firmware
        ).pack(side=LEFT)

        tb.Checkbutton(
            f, text="Erase entire flash first", variable=self.flash_erase_var, bootstyle="round-toggle"
        ).pack(anchor=W, pady=(4, 10))

        action_row = tb.Frame(f)
        action_row.pack(fill=X, pady=(0, 10))
        self.flash_btn = tb.Button(action_row, text="Flash", bootstyle="success", command=self._on_flash)
        self.flash_btn.pack(side=LEFT)
        self.flash_cancel_btn = tb.Button(
            action_row, text="Cancel", bootstyle="danger-outline", command=self._on_cancel_flash, state="disabled"
        )
        self.flash_cancel_btn.pack(side=LEFT, padx=(8, 0))

        self.flash_progress = tb.Progressbar(
            f, variable=self.flash_progress_var, maximum=100, bootstyle="success"
        )
        self.flash_progress.pack(fill=X, pady=(0, 10))

        log_frame = tb.Labelframe(f, text="esptool output", padding=6)
        log_frame.pack(fill=BOTH, expand=True)
        self.flash_log = tk.Text(log_frame, height=14, wrap="word", state="disabled")
        self.flash_log.pack(fill=BOTH, expand=True)

        tb.Label(
            f,
            text="On Linux, your user may need to be in the 'dialout' group to access serial ports "
                 "(e.g. `sudo usermod -aG dialout $USER`, then log out and back in) - never run this "
                 "app as root/sudo to work around a permission error instead.",
            bootstyle="secondary", wraplength=760, justify=LEFT,
        ).pack(anchor=W, pady=(8, 0))

        self._refresh_serial_ports()

    def _refresh_serial_ports(self):
        ports = flashing.list_serial_ports()
        self._flash_ports_by_label = {}
        labels = []
        default_label = None
        for p in ports:
            label = f"{p.device} - {p.description}" + (" (likely M5Stack)" if p.likely else "")
            labels.append(label)
            self._flash_ports_by_label[label] = p.device
            if p.likely and default_label is None:
                default_label = label

        self.flash_port_combo["values"] = labels
        if labels:
            self.flash_port_var.set(default_label or labels[0])
        else:
            self.flash_port_var.set("")

    def _on_browse_flash_firmware(self):
        path = filedialog.askopenfilename(
            title="Select firmware .bin", filetypes=[("Firmware binary", "*.bin"), ("All files", "*.*")],
        )
        if path:
            self.flash_file_var.set(path)

    def _on_flash(self):
        port = self._flash_ports_by_label.get(self.flash_port_var.get())
        file_path = self.flash_file_var.get().strip()
        if not port:
            Messagebox.show_warning("Select a serial port first.", "m5uploader")
            return
        if not file_path:
            Messagebox.show_warning("Select a firmware .bin file first.", "m5uploader")
            return
        if Messagebox.yesno(
            f"This will overwrite the firmware on {port}. Continue?", "m5uploader", localize=False
        ) != "Yes":
            return

        self.flash_log.configure(state="normal")
        self.flash_log.delete("1.0", END)
        self.flash_log.configure(state="disabled")
        self.flash_progress_var.set(0)
        self.flash_btn.config(state="disabled")
        self.flash_cancel_btn.config(state="normal")

        erase = self.flash_erase_var.get()
        cancel_event = threading.Event()
        self._flash_cancel_event = cancel_event

        def worker():
            try:
                flashing.flash_firmware(
                    port, file_path, erase=erase,
                    on_line=lambda line: self.msg_queue.put(("flash_line", line)),
                    on_progress=lambda pct: self.msg_queue.put(("flash_progress", pct)),
                    cancel_event=cancel_event,
                )
                self.msg_queue.put(("flash_ok",))
            except flashing.FlashCancelled:
                self.msg_queue.put(("flash_cancelled",))
            except flashing.FlashError as exc:
                self.msg_queue.put(("flash_err", str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def _on_cancel_flash(self):
        if self._flash_cancel_event is not None:
            self._flash_cancel_event.set()
            self.flash_cancel_btn.config(state="disabled")

    # ------------------------------------------------------------------
    # update notice (no auto-updater - see update_check.py)
    # ------------------------------------------------------------------

    def _check_for_update(self):
        def worker():
            info = update_check.check_for_update(__version__)
            if info:
                self.msg_queue.put(("update_available", info))

        threading.Thread(target=worker, daemon=True).start()

    def _on_update_click(self, event=None):
        if self._update_url:
            webbrowser.open(self._update_url)


def _fix_frozen_tcl_modules():
    """Register PyInstaller's bundled Tcl Modules dir with Tcl itself.

    Debian/Ubuntu's Tcl 8.6 adds an extra module search root pointing at
    the literal, hardcoded path /usr/share/tcltk/tcl8.6/tcl8 - it isn't
    derived from TCL_LIBRARY at interpreter startup. PyInstaller bundles
    that same tcl8/ directory (nested under the library dir it collects,
    since that's where Debian ships it), but nothing on a frozen Linux
    build ever points Tcl at that bundled copy, so `package require
    msgcat` silently resolves to an older, built-in msgcat lacking
    `mcmset` (added in msgcat 1.6) - raising "invalid command name
    ::msgcat::mcmset" the moment ttkbootstrap initializes. This is a
    no-op on platforms where the directory isn't bundled (Windows/macOS
    don't hit this).

    tkinter.Tk.__init__ creates the bare Tcl interpreter via
    _tkinter.create() and only *then* loads Tk on top via _loadtk() - Tk
    pulls in msgcat as part of its own startup, through a path that
    doesn't appear to re-scan the module list the way a plain `package
    require` does. So it's not enough to just register the extra module
    root before calling the real _loadtk(): `package require msgcat`
    also has to be forced here, priming Tcl's package cache with the
    correct (bundled) version before Tk's own internal requirement runs
    and quietly reuses whatever's already cached.
    """
    if not getattr(sys, "frozen", False):
        return
    meipass = getattr(sys, "_MEIPASS", None)
    if not meipass:
        return
    bundled_tm_dir = os.path.join(meipass, "_tcl_data", "tcl8")
    if not os.path.isdir(bundled_tm_dir):
        return
    original_loadtk = tk.Tk._loadtk

    def patched_loadtk(self):
        self.tk.eval(f"::tcl::tm::path add {{{bundled_tm_dir}}}")
        try:
            self.tk.eval("package require msgcat")
        except tk.TclError:
            pass
        original_loadtk(self)

    tk.Tk._loadtk = patched_loadtk


def main():
    _fix_frozen_tcl_modules()
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
