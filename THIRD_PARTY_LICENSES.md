# Third-party licenses

m5uploader itself is licensed under the [Apache License 2.0](LICENSE). Its
Python dependencies are permissively licensed (MIT/BSD/Apache-style),
**with one deliberate exception**, documented here for transparency.

## esptool (GPLv2 or later)

[esptool](https://github.com/espressif/esptool) is Espressif's official
ESP32 flashing tool, licensed **GPLv2 or later**. m5uploader uses it to
implement on-device flashing (`m5uploader/flashing.py`).

Importing esptool as a Python library directly into the m5uploader process
would mix GPL-licensed code into the same running process as this
Apache-2.0 codebase, which risks pulling the whole distributed binary
under the GPL's combined-work provisions.

To avoid that, esptool is **never imported by the main m5uploader
process**. It is only ever invoked as a separate, isolated subprocess:

- Running from source: `python -m esptool ...` - a distinct OS process
  from the main app, even though both share the same virtualenv.
- Frozen (packaged) builds: a second, separate executable
  (`m5uploader-esptool[.exe]`), built independently from
  `esptool_helper.py` and shipped alongside the main m5uploader binary
  (see `.github/workflows/build.yml`). `m5uploader/flashing.py` invokes it
  by path and communicates only via argv, stdout, and exit code.

This is the same "mere aggregation" pattern used by many projects that
depend on GPL-licensed command-line tools (e.g. subprocessing `ffmpeg` or
`git` rather than linking their GPL-licensed libraries) specifically to
keep the calling application's own license unaffected. m5uploader's own
source remains entirely Apache-2.0; the bundled esptool helper is,
separately, GPLv2+ - its full source is available at
<https://github.com/espressif/esptool>.

## Other dependencies

See `requirements.txt` for the full list. At the time of writing:
`requests` (Apache-2.0), `ttkbootstrap` (MIT), `Pillow` (MIT-CMU),
`packaging` (Apache-2.0 or BSD-2-Clause), `pyserial` (BSD-3-Clause),
`keyring` (MIT), `secretstorage` (BSD-3-Clause, Linux only).
