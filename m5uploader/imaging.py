"""Local image processing utilities. No network/API concerns here -
see api.py for what actually talks to M5Stack."""

import io
from pathlib import Path

from PIL import Image

MAX_COVER_BYTES = 500_000


def compress_cover(path: str, max_bytes: int = MAX_COVER_BYTES):
    """Re-encode a cover image so it's under max_bytes.

    Keeps PNG (only shrinking dimensions, since PNG has no quality knob)
    if the source has an alpha channel, otherwise re-encodes as JPEG and
    ratchets quality down before falling back to shrinking dimensions.
    Returns (bytes, filename, mimetype).
    """
    image = Image.open(path)
    image.load()
    has_alpha = image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info)

    def encode(im, quality):
        buf = io.BytesIO()
        if has_alpha:
            im.save(buf, format="PNG", optimize=True)
        else:
            im.convert("RGB").save(buf, format="JPEG", quality=quality, optimize=True)
        return buf.getvalue()

    quality = 90
    current = image
    data = encode(current, quality)
    while len(data) > max_bytes:
        if not has_alpha and quality > 30:
            quality -= 15
        else:
            w, h = current.size
            if min(w, h) <= 64:
                break
            current = current.resize((max(1, int(w * 0.8)), max(1, int(h * 0.8))), Image.LANCZOS)
        data = encode(current, quality)

    ext = "png" if has_alpha else "jpg"
    mimetype = "image/png" if has_alpha else "image/jpeg"
    return data, f"{Path(path).stem}.{ext}", mimetype
