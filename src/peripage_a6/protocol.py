"""Wire encoding for the PeriPage A6 304dpi.

The printer is not a full ESC/POS device. Raster output uses the ESC/POS
``GS v 0`` bitmap command; paper feed uses ``ESC J``. Everything else is a
vendor opcode prefixed with ``10 FF``.

This module is pure: it builds bytes. It never touches a socket.
"""

from __future__ import annotations

from enum import IntEnum

from .models import A6_304, Model

VENDOR = b"\x10\xff"

# Session / configuration
CMD_RESET = bytes.fromhex("10fffe01") + bytes(12)
CMD_END_JOB = bytes.fromhex("10fffe45")
CMD_CONCENTRATION = bytes.fromhex("10ff1000")  # + 1 byte level
CMD_POWER_TIMEOUT = bytes.fromhex("10ff12")  # + uint16 BE minutes

# Queries. Each returns a short ASCII or binary payload.
CMD_QUERY_MODEL = bytes.fromhex("10ff20f0")  # e.g. b"IP-300"
CMD_QUERY_FIRMWARE = bytes.fromhex("10ff20f1")  # e.g. b"V2.11_304dpi"
CMD_QUERY_SERIAL = bytes.fromhex("10ff20f2")
CMD_QUERY_BATTERY = bytes.fromhex("10ff50f1")  # b"\x00" + percent
CMD_QUERY_HARDWARE = bytes.fromhex("10ff3010")
CMD_QUERY_NAME = bytes.fromhex("10ff3011")
CMD_QUERY_MAC = bytes.fromhex("10ff3012")

# ESC/POS subset
ESC_J = bytes.fromhex("1b4a")  # relative paper feed, 1 byte n
GS_V_0 = bytes.fromhex("1d7630")  # raster bit image

# ``10 FF 70 F1 00`` returns a combined status string but also corrupts the
# next print (horizontal shift + a block character in the ASCII buffer).
CMD_QUERY_FULL = bytes.fromhex("10ff70f100")

MAX_FEED_DOTS = 0xFF
MAX_RASTER_ROWS = 0xFFFF
SAFE_CHUNK_ROWS = 255


class Concentration(IntEnum):
    """Print-head energy. Higher is darker and hotter."""

    LIGHT = 0
    MEDIUM = 1
    DARK = 2


def reset() -> bytes:
    """Required after connect. The printer stays mute until this is sent."""
    return CMD_RESET


def end_job() -> bytes:
    """Vendor trailer used by the original A6 Bluetooth client after a feed."""
    return CMD_END_JOB


def set_concentration(level: int | Concentration) -> bytes:
    value = int(level)
    if value not in (0, 1, 2):
        raise ValueError("concentration must be 0, 1, or 2")
    return CMD_CONCENTRATION + bytes([value])


def set_power_timeout(minutes: int) -> bytes:
    minutes = max(1, min(0xFFF0, int(minutes)))
    return CMD_POWER_TIMEOUT + minutes.to_bytes(2, "big")


def feed(dots: int) -> bytes:
    """Advance paper by ``dots`` (1–255). 255 is about 21 mm at 304 dpi."""
    dots = max(1, min(MAX_FEED_DOTS, int(dots)))
    return ESC_J + bytes([dots])


def raster_header(height: int, model: Model = A6_304) -> bytes:
    """Build a ``GS v 0`` preamble for ``height`` rows of packed bitmap data.

    Layout: ``1D 76 30 m xL xH yL yH``
    ``m=0`` normal size, ``x`` = bytes/row, ``y`` = rows, both little-endian.
    """
    if height < 1 or height > MAX_RASTER_ROWS:
        raise ValueError(f"height must be 1..{MAX_RASTER_ROWS}, got {height}")
    x = model.row_bytes
    return GS_V_0 + bytes(
        [
            0x00,  # m: normal
            x & 0xFF,
            (x >> 8) & 0xFF,
            height & 0xFF,
            (height >> 8) & 0xFF,
        ]
    )


def raster_chunk(payload: bytes, model: Model = A6_304) -> bytes:
    """Preamble plus packed rows. ``payload`` length must be a multiple of row_bytes."""
    if len(payload) % model.row_bytes != 0:
        raise ValueError(
            f"payload length {len(payload)} is not a multiple of {model.row_bytes}"
        )
    height = len(payload) // model.row_bytes
    if height == 0:
        return b""
    return raster_header(height, model) + payload


def pad_row(row: bytes, model: Model = A6_304) -> bytes:
    """Truncate or zero-pad a single packed row to the print-head width."""
    n = model.row_bytes
    if len(row) < n:
        return row.ljust(n, b"\x00")
    return row[:n]


def split_chunks(payload: bytes, chunk_rows: int = SAFE_CHUNK_ROWS, model: Model = A6_304) -> list[bytes]:
    """Slice packed image data into protocol-sized raster commands.

    The printer's internal buffer is only a few hundred rows. Sending more
    than ``SAFE_CHUNK_ROWS`` in one ``GS v 0`` command drops data.
    """
    return [header + b"".join(rows) for header, rows in iter_raster_packets(payload, chunk_rows, model)]


def iter_raster_packets(
    payload: bytes,
    chunk_rows: int = SAFE_CHUNK_ROWS,
    model: Model = A6_304,
) -> list[tuple[bytes, list[bytes]]]:
    """Yield ``(GS v 0 header, [row, ...])`` packets for one BLE write each.

    Concatenating the job and slicing by BLE MTU makes this printer swallow
    the bytes and print nothing. Command boundaries must stay intact.
    """
    if len(payload) % model.row_bytes != 0:
        raise ValueError(
            f"payload length {len(payload)} is not a multiple of {model.row_bytes}"
        )
    if chunk_rows < 1 or chunk_rows > MAX_RASTER_ROWS:
        raise ValueError(f"chunk_rows must be 1..{MAX_RASTER_ROWS}")
    packets: list[tuple[bytes, list[bytes]]] = []
    stride = model.row_bytes * chunk_rows
    for i in range(0, len(payload), stride):
        chunk = payload[i : i + stride]
        if not chunk:
            continue
        height = len(chunk) // model.row_bytes
        rows = [chunk[j : j + model.row_bytes] for j in range(0, len(chunk), model.row_bytes)]
        packets.append((raster_header(height, model), rows))
    return packets


def filter_ascii(text: str) -> str:
    """Drop bytes the built-in font cannot print (keep space through ``~``)."""
    return "".join(ch for ch in text if 32 <= ord(ch) < 127)


def ascii_line(text: str, model: Model = A6_304) -> bytes:
    """Encode one line for the built-in 7-bit ASCII font.

    The firmware freezes if two consecutive ``\\n`` bytes land in its text
    buffer, so this never emits a blank newline-only payload.
    """
    cleaned = filter_ascii(text)[: model.ascii_columns]
    if not cleaned:
        return b""
    return cleaned.encode("ascii") + b"\n"


def decode_ascii(payload: bytes) -> str:
    """Decode a query response, stripping NULs and surrounding whitespace."""
    return payload.replace(b"\x00", b"").decode("ascii", errors="replace").strip()


def decode_battery(payload: bytes) -> int:
    """Battery query returns ``{0, percent}``."""
    if len(payload) < 2:
        raise ValueError(f"short battery response: {payload!r}")
    return int(payload[1])


def decode_mac(payload: bytes) -> str:
    """MAC query returns the six address bytes, often duplicated with junk."""
    raw = payload[:6]
    if len(raw) < 6:
        raise ValueError(f"short MAC response: {payload!r}")
    return ":".join(f"{b:02X}" for b in raw)
