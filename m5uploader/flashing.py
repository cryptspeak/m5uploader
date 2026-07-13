"""On-device firmware flashing for m5uploader.

Talks to esptool (Espressif's official ESP32 flashing tool) exclusively as
an isolated subprocess - never imported into this process. esptool is
GPLv2+; this app is Apache-2.0. Importing esptool's Python API directly
into the same running process would mix GPL code into this process and
risks pulling the whole distributed binary under the GPL. Invoking it as a
separate process, communicating only via argv/stdout/exit code, is the
standard way projects avoid that entanglement (the same reason many tools
subprocess `ffmpeg` or `git` rather than link their GPL libraries) - see
THIRD_PARTY_LICENSES.md.

`resolve_esptool_argv()` is the only place that knows how to reach esptool:
`python -m esptool` (a normal, pip-installed subprocess call - still a
separate process even though it shares a venv) when running from source,
or a bundled sibling helper executable when frozen (built and shipped by
the release workflow, see build.yml).

No sudo/elevation is ever attempted here. A serial port permission error
(e.g. the user not being in the `dialout` group on Linux) is surfaced as
a clear message, not worked around - consistent with this project's "no
OS-specific shell-outs" principle elsewhere.
"""

import re
import subprocess
import sys
import threading
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from serial.tools import list_ports

# Confirmed USB-serial bridge chips used on M5Stack boards: Silicon Labs
# CP210x (most Core/Core2/CoreS3 units) and WCH CH9102/CH340 (newer/other
# units). Used only to sort the "likely" device to the top of the port
# list - never to filter out other ports, since plenty of legitimate
# devices/clones use different bridges.
_LIKELY_VID_PID = {
    (0x10C4, 0xEA60),  # Silicon Labs CP210x
    (0x1A86, 0x55D4),  # WCH CH9102
    (0x1A86, 0x7523),  # WCH CH340
}

# esptool's own progress bar renders as e.g.
# "Writing at 0x00012c90 [====>          ]  24.0% 245760/1023947 bytes..."
# - a percentage (with a decimal point, not always an integer), not the
# older parenthesised "(NN %)" form. It's redrawn many times a second via
# ANSI cursor-movement codes (\x1b[1A / \x1b[2K / \x1b[K) that only make
# sense on a real terminal; captured through a pipe, each redraw arrives
# as its own separate line full of escape-code noise. _ANSI_RE strips
# that noise; _PROGRESS_LINE_RE identifies (post-strip) lines that are
# just one of these redraw ticks, so they can drive the progress bar
# without also spamming the visible log with dozens of near-identical
# lines - the log stays readable, the numeric bar still updates smoothly.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_PROGRESS_RE = re.compile(r"(\d{1,3}(?:\.\d+)?)\s*%")
_PROGRESS_LINE_RE = re.compile(r"^Writing at 0x[0-9A-Fa-f]+")


def _clean_line(raw: str) -> str:
    return _ANSI_RE.sub("", raw).replace("\r", "").strip()

HELPER_NAME = "m5uploader-esptool.exe" if sys.platform == "win32" else "m5uploader-esptool"


class FlashError(Exception):
    pass


class FlashCancelled(FlashError):
    pass


@dataclass(frozen=True)
class SerialPort:
    device: str
    description: str
    likely: bool


def list_serial_ports() -> list:
    ports = [
        SerialPort(
            device=p.device,
            description=p.description or p.device,
            likely=(p.vid, p.pid) in _LIKELY_VID_PID,
        )
        for p in list_ports.comports()
    ]
    ports.sort(key=lambda sp: (not sp.likely, sp.device))
    return ports


def resolve_esptool_argv() -> list:
    """Subprocess argv *prefix* used to invoke esptool as an isolated
    process. Never imported - see module docstring."""
    if getattr(sys, "frozen", False):
        helper = Path(sys.executable).resolve().parent / HELPER_NAME
        if not helper.exists():
            raise FlashError(
                f"Flashing helper not found ({helper}). This build may be "
                "missing the bundled esptool helper."
            )
        return [str(helper)]
    return [sys.executable, "-m", "esptool"]


def flash_firmware(
    port: str,
    file_path: str,
    *,
    erase: bool = False,
    offset: int = 0x0,
    on_line=None,
    on_progress=None,
    cancel_event: threading.Event = None,
) -> None:
    """Flash `file_path` to `port` at `offset` (default 0x0 - M5Stack
    catalog binaries are pre-merged full images, same as what M5Burner
    flashes). `on_line(str)` is called for each ANSI-stripped, meaningful
    line of esptool's own output (shown live in the GUI, so the user sees
    exactly what's happening on their device - no black box); the
    high-frequency per-chunk progress redraw lines are deliberately not
    forwarded here (see `_PROGRESS_LINE_RE`) since `on_progress(float)`,
    called with a 0-100 percentage parsed from the same lines, already
    covers them via the GUI's own progress bar. Raises FlashCancelled if
    `cancel_event` was set, FlashError on any other failure."""
    argv = resolve_esptool_argv() + ["--port", port, "write-flash"]
    if erase:
        argv.append("--erase-all")
    argv += [hex(offset), str(file_path)]

    try:
        proc = subprocess.Popen(
            argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
        )
    except OSError as exc:
        raise FlashError(f"Could not start esptool: {exc}") from exc

    watcher = None
    if cancel_event is not None:
        def _watch_cancel():
            while proc.poll() is None:
                if cancel_event.wait(timeout=0.2):
                    if proc.poll() is None:
                        proc.terminate()
                    return

        watcher = threading.Thread(target=_watch_cancel, daemon=True)
        watcher.start()

    tail = deque(maxlen=40)
    for raw_line in proc.stdout:
        line = _clean_line(raw_line)
        if not line:
            continue
        tail.append(line)

        match = _PROGRESS_RE.search(line)
        is_progress_line = bool(_PROGRESS_LINE_RE.match(line))
        if match and is_progress_line and on_progress:
            on_progress(min(100.0, float(match.group(1))))
        if on_line and not is_progress_line:
            on_line(line)

    proc.wait()
    if watcher:
        watcher.join(timeout=1)

    if cancel_event is not None and cancel_event.is_set():
        raise FlashCancelled("Flashing cancelled.")
    if proc.returncode != 0:
        raise FlashError(f"esptool exited with status {proc.returncode}:\n" + "\n".join(tail))
