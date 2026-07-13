"""Local image processing utilities. No network/API concerns here -
see api.py for what actually talks to M5Stack."""

import io
from pathlib import Path

from PIL import Image, ImageOps

MAX_COVER_BYTES = 500_000


def compress_cover(path: str, max_bytes: int = MAX_COVER_BYTES):
    """Re-encode a cover image so it's under max_bytes.

    Keeps PNG (only shrinking dimensions, since PNG has no quality knob)
    if the source has an alpha channel, otherwise re-encodes as JPEG and
    ratchets quality down before falling back to shrinking dimensions.
    Returns (bytes, filename, mimetype).

    Deliberately strips all metadata (EXIF, ICC profile, XMP, text
    chunks) from the output - a cover photo taken on a phone can carry
    GPS coordinates in its EXIF data, which would otherwise get uploaded
    and made public without the user ever seeing it. `image.info` is
    cleared explicitly and `encode()` below never passes an `exif=`,
    `icc_profile=`, `pnginfo=`, or `comment=` kwarg to `save()` - Pillow
    only ever writes those if a caller hands them to `save()` directly,
    it never copies `image.info` across automatically, so this is a real
    strip, not reliance on a save()-doesn't-bother default. exif_transpose
    runs first so a photo relying on an EXIF orientation tag for correct
    display doesn't come out sideways once that tag is gone.
    """
    image = Image.open(path)
    image.load()
    image = ImageOps.exif_transpose(image) or image
    image.info = {}
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
