from PIL import Image

from peripage_a6.printer import Printer
from peripage_a6.protocol import (
    CMD_END_JOB,
    CMD_QUERY_BATTERY,
    CMD_QUERY_FIRMWARE,
    CMD_QUERY_HARDWARE,
    CMD_QUERY_MAC,
    CMD_QUERY_MODEL,
    CMD_QUERY_NAME,
    CMD_QUERY_SERIAL,
    Concentration,
    reset,
    set_concentration,
)
from peripage_a6.raster import Dither
from peripage_a6.transport import DummyTransport


def _printer(replies=None) -> tuple[Printer, DummyTransport]:
    transport = DummyTransport(replies)
    printer = Printer(transport, pace_s=0.0, chunk_pause_s=0.0, settle_s=0.0)
    return printer, transport


def test_connect_sends_reset():
    printer, transport = _printer()
    printer.connect()
    assert transport.writes[0] == reset()
    printer.disconnect()
    assert transport.opened is False


def test_print_black_row_job_shape():
    printer, transport = _printer()
    image = Image.new("RGB", (576, 1), (0, 0, 0))
    with printer:
        printer.print_image(
            image,
            dither=Dither.THRESHOLD,
            concentration=Concentration.DARK,
            feed_dots=64,
        )
    sent = transport.sent
    assert sent.startswith(reset())  # connect
    assert set_concentration(Concentration.DARK) in transport.writes
    assert bytes.fromhex("1d76300048000100") in sent
    assert bytes.fromhex("1b4a40") in sent
    assert CMD_END_JOB in transport.writes
    raster = bytes.fromhex("1d76300048000100") + (b"\xff" * 72)
    assert raster in sent
    header = bytes.fromhex("1d76300048000100")
    assert header in transport.writes
    assert (b"\xff" * 72) in transport.writes


def test_info_parses_query_replies():
    replies = {
        CMD_QUERY_NAME: b"PeriPage+DF7A",
        CMD_QUERY_FIRMWARE: b"V2.11_304dpi",
        CMD_QUERY_SERIAL: b"A6491571121",
        CMD_QUERY_HARDWARE: b"BR2141e-s(A02)_B9_20190815_r3460",
        CMD_QUERY_MODEL: b"IP-300",
        CMD_QUERY_MAC: bytes.fromhex("00f57325ac9f"),
        CMD_QUERY_BATTERY: b"\x00\x54",
    }
    printer, _transport = _printer(replies)
    with printer:
        info = printer.info()
    assert info.name == "PeriPage+DF7A"
    assert info.firmware == "V2.11_304dpi"
    assert info.serial == "A6491571121"
    assert info.model_id == "IP-300"
    assert info.mac == "00:F5:73:25:AC:9F"
    assert info.battery == 84


def test_ascii_wraps_at_48_columns():
    printer, transport = _printer()
    with printer:
        printer.print_ascii("A" * 50, feed_dots=1, line_delay_s=0)
    lines = [w for w in transport.writes if w.endswith(b"\n") and w[:1] == b"A"]
    assert lines[0] == b"A" * 48 + b"\n"
    assert lines[1] == b"AA\n"
