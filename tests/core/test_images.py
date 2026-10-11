import io
import struct
import zlib

import pytest
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image

from apps.core.images import clean_image

ALLOWED = {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG", ".webp": "WEBP"}


def image_bytes(size=(4, 4), fmt="PNG", mode="RGB", color="red"):
    buf = io.BytesIO()
    Image.new(mode, size, color).save(buf, fmt)
    return buf.getvalue()


def photo(upload, **over):
    opts = dict(
        max_bytes=2 * 1024 * 1024,
        allowed=ALLOWED,
        max_pixels=4096 * 4096,
        output="jpeg",
        max_side=600,
        quality=80,
    )
    opts.update(over)
    return clean_image(upload, **opts)


def same(upload, **over):
    return photo(upload, **{"output": "same", "max_side": None, "quality": 90, **over})


def test_jpeg_output_resized_and_renamed():
    out = photo(SimpleUploadedFile("Me.PNG", image_bytes((1200, 900))))
    img = Image.open(io.BytesIO(out.read()))
    assert img.format == "JPEG" and img.size == (600, 450)
    assert out.name == "Me.jpg"


def test_small_image_not_enlarged():
    out = photo(SimpleUploadedFile("a.jpg", image_bytes((50, 30), "JPEG")))
    assert Image.open(io.BytesIO(out.read())).size == (50, 30)


@pytest.mark.parametrize("mode", ["RGBA", "LA", "P"])
def test_transparent_image_flattened_on_white(mode):
    img = Image.new("RGBA", (20, 20), (0, 0, 0, 0))
    img.putpixel((0, 0), (255, 0, 0, 255))
    if mode != "RGBA":
        img = img.convert(mode)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    out = photo(SimpleUploadedFile("t.png", buf.getvalue()))
    result = Image.open(io.BytesIO(out.read()))
    assert result.mode == "RGB"
    r, g, b = result.getpixel((15, 15))
    assert min(r, g, b) > 240  # transparent area became white, not black


def test_same_output_keeps_format_and_strips_trailing_junk():
    out = same(SimpleUploadedFile("a.png", image_bytes() + b"JUNK" * 100))
    data = out.read()
    assert b"JUNK" not in data and Image.open(io.BytesIO(data)).format == "PNG"
    assert out.name == "a.png"


@pytest.mark.parametrize(
    "name,content,message",
    [
        ("a.gif", image_bytes(fmt="GIF"), ".png, .jpg, .jpeg or .webp"),
        ("a.png", image_bytes(fmt="JPEG"), "does not match"),
        ("a.png", b"not an image", "not a valid image"),
        ("a.png", b"x" * (2 * 1024 * 1024 + 1), "2 MB or smaller"),
    ],
)
def test_rejections(name, content, message):
    with pytest.raises(ValidationError) as exc:
        photo(SimpleUploadedFile(name, content))
    assert message in str(exc.value)


def _png_declaring(width, height):
    raw = bytearray(image_bytes())
    raw[16:24] = struct.pack(">II", width, height)
    raw[29:33] = struct.pack(">I", zlib.crc32(bytes(raw[12:29])))
    return bytes(raw)


def test_huge_declared_canvas_rejected():
    with pytest.raises(ValidationError) as exc:
        photo(SimpleUploadedFile("a.png", _png_declaring(5000, 5000)))
    assert "4096" in str(exc.value)


def test_label_used_in_messages():
    with pytest.raises(ValidationError) as exc:
        same(SimpleUploadedFile("a.gif", b"x"), label="Logo", max_bytes=1_048_576)
    assert str(exc.value.messages[0]) == "Logo must be a .png, .jpg, .jpeg or .webp file."
