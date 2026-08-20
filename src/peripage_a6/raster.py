"""Convert images to the 1-bit packed rows the A6 304dpi print head expects."""

from __future__ import annotations

from enum import Enum

from PIL import Image, ImageEnhance, ImageOps

from .models import A6_304, Model
from .protocol import pad_row


class Dither(str, Enum):
    FLOYD_STEINBERG = "floyd-steinberg"
    ATKINSON = "atkinson"
    THRESHOLD = "threshold"


def _atkinson(gray: Image.Image) -> Image.Image:
    """Atkinson dither. Spreads 6/8 of the error; slightly lighter than Floyd-Steinberg."""
    pixels = [float(p) for p in gray.getdata()]
    width, height = gray.size
    for y in range(height):
        for x in range(width):
            i = y * width + x
            old = pixels[i]
            new = 0.0 if old < 128.0 else 255.0
            pixels[i] = new
            err = (old - new) / 8.0
            for dx, dy in ((1, 0), (2, 0), (-1, 1), (0, 1), (1, 1), (0, 2)):
                nx, ny = x + dx, y + dy
                if 0 <= nx < width and 0 <= ny < height:
                    j = ny * width + nx
                    pixels[j] = max(0.0, min(255.0, pixels[j] + err))
    out = Image.new("L", gray.size)
    out.putdata([0 if p < 128 else 255 for p in pixels])
    return out.convert("1")


def prepare_image(
    image: Image.Image,
    *,
    model: Model = A6_304,
    dither: Dither = Dither.FLOYD_STEINBERG,
    brightness: float = 1.0,
    contrast: float = 1.0,
    rotate: int = 0,
    threshold: int = 128,
) -> Image.Image:
    """Resize to native width and convert to 1-bit, inverted for the thermal head.

    Thermal printers burn where the bit is 1. Pillow mode ``1`` stores white as 1,
    so the image is inverted while still grayscale: originally-dark pixels become
    1-bits after conversion.
    """
    if rotate:
        image = image.rotate(rotate, expand=True, fillcolor="white")

    image = image.convert("L")
    if brightness != 1.0:
        image = ImageEnhance.Brightness(image).enhance(brightness)
    if contrast != 1.0:
        image = ImageEnhance.Contrast(image).enhance(contrast)

    src_w, src_h = image.size
    if src_w <= 0 or src_h <= 0:
        raise ValueError("image has empty dimensions")
    height = max(1, round(model.row_width * src_h / src_w))
    image = image.resize((model.row_width, height), Image.Resampling.LANCZOS)
    image = ImageOps.invert(image)

    if dither is Dither.THRESHOLD:
        return image.point(lambda p, t=threshold: 255 if p >= t else 0, mode="1")
    if dither is Dither.ATKINSON:
        return _atkinson(image)
    return image.convert("1", dither=Image.Dither.FLOYD_STEINBERG)


def pack_bitmap(image: Image.Image, model: Model = A6_304) -> bytes:
    """Pack a mode-``1`` image into ``row_bytes``-aligned rows, MSB leftmost."""
    if image.mode != "1":
        raise ValueError(f"expected mode '1', got {image.mode!r}")
    width, height = image.size
    if width != model.row_width:
        raise ValueError(f"expected width {model.row_width}, got {width}")
    packed = image.tobytes()
    # Pillow may pad each row to a full byte already; re-slice to be sure.
    src_row_bytes = (width + 7) // 8
    rows = []
    for y in range(height):
        start = y * src_row_bytes
        rows.append(pad_row(packed[start : start + src_row_bytes], model))
    return b"".join(rows)


def image_to_raster(
    image: Image.Image,
    *,
    model: Model = A6_304,
    dither: Dither = Dither.FLOYD_STEINBERG,
    brightness: float = 1.0,
    contrast: float = 1.0,
    rotate: int = 0,
    threshold: int = 128,
) -> bytes:
    prepared = prepare_image(
        image,
        model=model,
        dither=dither,
        brightness=brightness,
        contrast=contrast,
        rotate=rotate,
        threshold=threshold,
    )
    return pack_bitmap(prepared, model)


def pack_row_pixels(pixels: bytes | bytearray | list[int], model: Model = A6_304) -> bytes:
    """Pack 0/1 pixel values (1 = black / burn) into one print-head row."""
    row = bytearray(model.row_bytes)
    limit = min(len(pixels), model.row_width)
    for x in range(limit):
        if pixels[x]:
            row[x // 8] |= 0x80 >> (x % 8)
    return bytes(row)


def render_text(
    text: str,
    *,
    model: Model = A6_304,
    font_path: str | None = None,
    font_size: int = 28,
    margin: int = 8,
    line_spacing: float = 1.25,
) -> Image.Image:
    """Render UTF-8 text to a white image the width of the print head."""
    from PIL import ImageDraw, ImageFont

    font = _load_font(font_path, font_size)
    dummy = Image.new("L", (model.row_width, 8), 255)
    draw = ImageDraw.Draw(dummy)
    max_width = model.row_width - 2 * margin
    lines = _wrap_text(text, draw, font, max_width)
    if not lines:
        lines = [""]

    line_h = _line_height(draw, font)
    gap = max(0, int(line_h * (line_spacing - 1.0)))
    height = margin * 2 + len(lines) * line_h + max(0, len(lines) - 1) * gap
    image = Image.new("RGB", (model.row_width, max(height, line_h + 2 * margin)), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    y = margin
    for line in lines:
        draw.text((margin, y), line, font=font, fill=(0, 0, 0))
        y += line_h + gap
    return image


def _load_font(font_path: str | None, size: int):
    from PIL import ImageFont

    candidates = [font_path] if font_path else []
    candidates.extend(
        [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/TTF/DejaVuSans.ttf",
            "/usr/share/fonts/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
            "/usr/share/fonts/liberation/LiberationSans-Regular.ttf",
        ]
    )
    for path in candidates:
        if not path:
            continue
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _line_height(draw, font) -> int:
    bbox = draw.textbbox((0, 0), "Ag", font=font)
    return max(1, bbox[3] - bbox[1])


def _wrap_text(text: str, draw, font, max_width: int) -> list[str]:
    lines: list[str] = []
    for paragraph in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if paragraph == "":
            lines.append("")
            continue
        current = ""
        for word in paragraph.split(" "):
            candidate = word if not current else f"{current} {word}"
            bbox = draw.textbbox((0, 0), candidate, font=font)
            if bbox[2] - bbox[0] <= max_width or not current:
                current = candidate
            else:
                lines.append(current)
                current = word
        lines.append(current)
    return lines
