from peripage_a6.models import A6_304
from peripage_a6.transport import parse_gatt_characteristics
from peripage_a6.protocol import (
    Concentration,
    ascii_line,
    decode_battery,
    decode_mac,
    feed,
    iter_raster_packets,
    raster_chunk,
    raster_header,
    reset,
    set_concentration,
    set_power_timeout,
    split_chunks,
)


def test_reset_is_vendor_init_plus_twelve_zeros():
    assert reset() == bytes.fromhex("10fffe01") + bytes(12)
    assert len(reset()) == 16


def test_raster_header_one_row_is_gs_v0_72_bytes():
    # 1D 76 30 00 | 48 00 | 01 00  -> x=72, y=1, little-endian
    assert raster_header(1) == bytes.fromhex("1d76300048000100")


def test_raster_header_tall_uses_high_height_byte():
    assert raster_header(256) == bytes.fromhex("1d76300048000001")


def test_raster_chunk_prefixes_payload():
    payload = bytes(A6_304.row_bytes)
    blob = raster_chunk(payload)
    assert blob.startswith(bytes.fromhex("1d76300048000100"))
    assert blob[8:] == payload


def test_split_chunks_caps_rows():
    payload = bytes(A6_304.row_bytes * 300)
    chunks = split_chunks(payload, chunk_rows=255)
    assert len(chunks) == 2
    assert chunks[0][6] == 255  # yL
    assert chunks[1][6] == 45


def test_iter_raster_packets_keeps_row_boundaries():
    payload = b"\xff" * (A6_304.row_bytes * 2)
    packets = iter_raster_packets(payload, chunk_rows=255)
    assert len(packets) == 1
    header, rows = packets[0]
    assert header == bytes.fromhex("1d76300048000200")
    assert rows == [b"\xff" * 72, b"\xff" * 72]


def test_concentration_and_feed():
    assert set_concentration(0) == bytes.fromhex("10ff100000")
    assert set_concentration(Concentration.DARK) == bytes.fromhex("10ff100002")
    assert feed(64) == bytes.fromhex("1b4a40")
    assert feed(0) == bytes.fromhex("1b4a01")
    assert feed(999) == bytes.fromhex("1b4aff")


def test_power_timeout_is_big_endian_minutes():
    assert set_power_timeout(10) == bytes.fromhex("10ff12000a")


def test_ascii_line_strips_unsafe_and_adds_newline():
    assert ascii_line("Hi\x00!") == b"Hi!\n"
    assert ascii_line("   ") == b"   \n"
    assert ascii_line("") == b""
    assert ascii_line("é") == b""
    assert len(ascii_line("x" * 80)) == 49  # 48 columns + newline


def test_parse_gatttool_value_handles():
    listing = """
handle: 0x000e, char properties: 0x10, char value handle: 0x000f, uuid: 0000ff01-0000-1000-8000-00805f9b34fb
handle: 0x0011, char properties: 0x0c, char value handle: 0x0012, uuid: 0000ff02-0000-1000-8000-00805f9b34fb
"""
    found = parse_gatt_characteristics(listing)
    assert found["0000ff02-0000-1000-8000-00805f9b34fb"] == 0x0012
    assert found["0000ff01-0000-1000-8000-00805f9b34fb"] == 0x000F


def test_decode_battery_and_mac():
    assert decode_battery(b"\x00\x54") == 84
    assert decode_mac(bytes.fromhex("00f57325ac9f") + b"junk") == "00:F5:73:25:AC:9F"
