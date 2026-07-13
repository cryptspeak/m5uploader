"""PyInstaller entry point for the bundled esptool helper executable.

Built as a second, separate binary from the main m5uploader app (see
build.yml) and shipped alongside it. m5uploader.flashing invokes this
executable as a subprocess - it never imports esptool directly. Keeping
esptool (GPLv2+) in its own executable, invoked only via subprocess/argv,
keeps it a clearly separate, isolated process from the Apache-2.0
m5uploader binary. See THIRD_PARTY_LICENSES.md.
"""

import esptool

if __name__ == "__main__":
    esptool.main()
