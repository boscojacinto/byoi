"""Print slips on the A6 or dump PNG/protocol for tests."""

from __future__ import annotations

import os
from pathlib import Path

from PIL import Image

from peripage_a6 import DumpTransport, Printer, open_ble_transport
from peripage_a6.raster import Dither


def print_slip(image: Image.Image, dest_dir: Path) -> dict[str, str]:
    dest_dir.mkdir(parents=True, exist_ok=True)
    png_path = dest_dir / "last-slip.png"
    image.save(png_path)

    dump_path = dest_dir / "last-slip.bin"
    mac = os.environ.get("PERIPAGE_MAC", "").strip()
    if mac:
        transport = open_ble_transport(mac)
        with Printer(transport) as printer:
            printer.print_image(image, dither=Dither.THRESHOLD, feed_dots=80)
        return {"mode": "live", "png": str(png_path), "mac": mac}

    transport = DumpTransport(dump_path)
    with Printer(transport, pace_s=0, chunk_pause_s=0, settle_s=0) as printer:
        printer.print_image(image, dither=Dither.THRESHOLD, feed_dots=80)
    return {"mode": "dump", "png": str(png_path), "bin": str(dump_path)}
