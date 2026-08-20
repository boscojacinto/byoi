from PIL import Image

from peripage_a6.models import A6_304
from peripage_a6.raster import Dither, image_to_raster, pack_row_pixels, prepare_image


def test_pack_row_msb_is_leftmost_pixel():
    pixels = [0] * A6_304.row_width
    pixels[0] = 1
    pixels[7] = 1
    pixels[8] = 1
    row = pack_row_pixels(pixels)
    assert len(row) == 72
    assert row[0] == 0b10000001
    assert row[1] == 0b10000000
    assert row[2] == 0


def test_black_image_becomes_all_ones():
    image = Image.new("RGB", (576, 4), (0, 0, 0))
    payload = image_to_raster(image, dither=Dither.THRESHOLD)
    assert len(payload) == 72 * 4
    assert payload == b"\xff" * len(payload)


def test_white_image_becomes_all_zeros():
    image = Image.new("RGB", (576, 4), (255, 255, 255))
    payload = image_to_raster(image, dither=Dither.THRESHOLD)
    assert payload == b"\x00" * (72 * 4)


def test_wide_image_is_scaled_to_native_width():
    image = Image.new("RGB", (1152, 10), (0, 0, 0))
    prepared = prepare_image(image, dither=Dither.THRESHOLD)
    assert prepared.size == (576, 5)


def test_narrow_image_keeps_aspect():
    image = Image.new("RGB", (288, 20), (0, 0, 0))
    prepared = prepare_image(image, dither=Dither.THRESHOLD)
    assert prepared.size[0] == 576
    assert prepared.size[1] == 40
