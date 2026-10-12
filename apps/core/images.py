"""Shared validate + re-encode step for uploaded images (school logos, student photos).

Rules (from the P1 logo pipeline):
- the extension must be allowed and the decoded format must match it;
- the upload, and the re-encoded result, must fit ``max_bytes``;
- the header's declared canvas is checked before any pixels are decoded, so a tiny
  compressed file cannot claim a huge canvas;
- the result is re-encoded from pixels only, dropping metadata and any trailing or
  embedded payload (no polyglots).
"""

import io
import math
import os
from typing import Literal

from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from PIL import Image, UnidentifiedImageError

_IMAGE_ERRORS = (
    UnidentifiedImageError,
    OSError,
    SyntaxError,
    ValueError,
    Image.DecompressionBombError,
    Image.DecompressionBombWarning,
)
_ALPHA_MODES = ("RGBA", "LA", "P", "PA", "RGBa", "La")


def _join(extensions: list[str]) -> str:
    return (
        extensions[0]
        if len(extensions) == 1
        else f"{', '.join(extensions[:-1])} or {extensions[-1]}"
    )


def _megabytes(n: int) -> str:
    mb = n / 1_048_576
    return f"{mb:g}"


def _to_rgb(img: Image.Image) -> Image.Image:
    """JPEG has no alpha channel: flatten transparent images onto white."""
    if img.mode in _ALPHA_MODES or "transparency" in img.info:
        rgba = img.convert("RGBA")
        background = Image.new("RGB", rgba.size, (255, 255, 255))
        background.paste(rgba, mask=rgba.getchannel("A"))
        return background
    if img.mode != "RGB":
        return img.convert("RGB")
    return img


def clean_image(
    upload,
    *,
    max_bytes: int,
    allowed: dict[str, str],
    max_pixels: int,
    output: Literal["same", "jpeg"],
    max_side: int | None,
    quality: int,
    label: str = "Image",
) -> ContentFile:
    """Validate ``upload`` and return a freshly encoded copy.

    ``allowed`` maps lower-case extensions to Pillow format names. ``max_pixels`` bounds the
    declared canvas: each side may be at most ``isqrt(max_pixels)`` (4096² → 4096×4096).
    ``output="jpeg"`` converts to an RGB JPEG named ``<stem>.jpg``; ``"same"`` keeps the
    format and name. ``max_side`` shrinks (never enlarges) to fit that square.
    Raises ``ValidationError`` with a user-facing message.
    """
    name = os.path.basename(getattr(upload, "name", "") or "")
    stem, ext = os.path.splitext(name)
    ext = ext.lower()
    if ext not in allowed:
        raise ValidationError(f"{label} must be a {_join(list(allowed))} file.")
    too_big = f"{label} must be {_megabytes(max_bytes)} MB or smaller."
    size = getattr(upload, "size", None)
    if size is not None and size > max_bytes:
        raise ValidationError(too_big)
    upload.seek(0)
    raw = upload.read()
    upload.seek(0)
    if len(raw) > max_bytes:
        raise ValidationError(too_big)
    expected = allowed[ext]
    side_limit = math.isqrt(max_pixels)
    try:
        Image.open(io.BytesIO(raw)).verify()
        img = Image.open(io.BytesIO(raw))
        if img.format != expected:
            raise ValidationError("The file content does not match its extension.")
        # Image.open only reads the header: refuse big canvases before decoding pixels.
        width, height = img.size
        if width > side_limit or height > side_limit:
            raise ValidationError(f"{label} must be at most {side_limit}×{side_limit} pixels.")
        img.load()
        # Re-encode from pixels only: drops metadata and any trailing/embedded payload.
        fresh = img.copy()
        if output == "jpeg":
            fresh = _to_rgb(fresh)
            fmt, out_name = "JPEG", f"{stem}.jpg"
            keep: dict = {}  # Pillow writes info["comment"] etc. into the output: drop all
        else:
            fmt, out_name = expected, name
            keep = {k: v for k, v in img.info.items() if k == "transparency"}
        if max_side is not None:
            fresh.thumbnail((max_side, max_side))
        fresh.info = keep
        options = {"quality": quality} if fmt in ("JPEG", "WEBP") else {}
        clean = io.BytesIO()
        fresh.save(clean, fmt, **options)
    except ValidationError:
        raise
    except _IMAGE_ERRORS:
        raise ValidationError("That file is not a valid image.") from None
    data = clean.getvalue()
    if len(data) > max_bytes:
        raise ValidationError(too_big)
    return ContentFile(data, name=out_name)
