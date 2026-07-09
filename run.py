"""Entry point for building a standalone executable with PyInstaller.

Running from source should use `python -m m5uploader` instead - this
file exists only because PyInstaller needs a plain script to point at.
"""

from m5uploader.gui import main

if __name__ == "__main__":
    main()
