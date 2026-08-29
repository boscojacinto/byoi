"""Print slips on the A6, queue them for the venue's relay, or dump for tests.

The PeriPage speaks Bluetooth LE, which is a property of the room. Moving the
desk to a cloud VM does not move the printer with it, so in ``relay`` mode the
desk composes the slip and leaves it in the queue for a small agent at the
counter (``scripts/print-relay.py``) to claim and print.
"""

from __future__ import annotations

import os
from pathlib import Path

from PIL import Image

from peripage_a6 import DumpTransport, Printer, open_ble_transport
from peripage_a6.raster import Dither

QUEUE_DIRNAME = "print-queue"


def print_mode() -> str:
    """``relay`` when the printer is somewhere else, ``local`` when it is here."""
    mode = os.environ.get("BYOI_PRINT_MODE", "").strip().lower()
    if mode in {"relay", "local"}:
        return mode
    return "local"


def queue_dir(dest_dir: Path) -> Path:
    path = Path(dest_dir) / QUEUE_DIRNAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def save_slip_png(image: Image.Image, dest_dir: Path, job_id: str) -> Path:
    """Keep the composed slip so the relay can fetch exactly what was queued."""
    path = queue_dir(dest_dir) / f"{job_id}.png"
    image.save(path, format="PNG")
    # The desk UI's "last slip" preview points here too.
    image.save(Path(dest_dir) / "last-slip.png")
    return path


def render_job(png_path: Path, dest_dir: Path) -> dict[str, str]:
    """Print a queued PNG on the printer attached to *this* machine.

    Used by the relay at the venue, and by a salon PC that still has the printer
    on its own Bluetooth.
    """
    image = Image.open(png_path)
    return print_slip(image, dest_dir)


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
