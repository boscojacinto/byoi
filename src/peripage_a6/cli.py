"""Command-line driver for the PeriPage A6 304dpi."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .printer import Printer, PrinterError
from .protocol import Concentration
from .raster import Dither
from .transport import (
    BleTransport,
    BluetoothTransport,
    DumpTransport,
    TransportError,
    normalize_address,
    open_ble_transport,
)


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return args.handler(args)
    except (PrinterError, TransportError, ValueError, FileNotFoundError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="peripage",
        description="Print to a PeriPage A6 304dpi thermal printer over Bluetooth LE.",
    )
    parser.add_argument("--version", action="version", version=f"peripage-a6 {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    discover = sub.add_parser("discover", help="list Bluetooth devices (PeriPage names highlighted)")
    discover.add_argument(
        "--scan",
        type=float,
        default=5.0,
        metavar="SECONDS",
        help="BLE inquiry duration (default 5; 0 = only already-known devices)",
    )
    discover.set_defaults(handler=_cmd_discover)

    info = sub.add_parser("info", help="query name, firmware, serial, battery")
    _add_target(info)
    info.set_defaults(handler=_cmd_info)

    feed = sub.add_parser("feed", help="advance paper without printing")
    _add_target(feed)
    feed.add_argument("--dots", type=int, default=64, help="feed distance, 1–255 per command (default 64)")
    feed.set_defaults(handler=_cmd_feed)

    print_cmd = sub.add_parser("print", help="print an image, rendered text, ASCII, or a QR code")
    _add_target(print_cmd)
    source = print_cmd.add_mutually_exclusive_group(required=True)
    source.add_argument("image", nargs="?", help="image file (PNG, JPEG, …)")
    source.add_argument("--text", help="UTF-8 text rendered to a bitmap")
    source.add_argument("--ascii", help="printer's built-in 7-bit font, 48 columns")
    source.add_argument("--qr", help="string encoded as a QR code")
    print_cmd.add_argument(
        "--dither",
        choices=[d.value for d in Dither],
        default=Dither.FLOYD_STEINBERG.value,
        help="halftone method for images (default floyd-steinberg)",
    )
    print_cmd.add_argument(
        "--concentration",
        choices=["light", "medium", "dark", "0", "1", "2"],
        default="medium",
        help="print energy / darkness",
    )
    print_cmd.add_argument("--feed", type=int, default=80, metavar="DOTS", help="paper feed after the job")
    print_cmd.add_argument("--rotate", type=int, choices=(0, 90, 180, 270), default=0)
    print_cmd.add_argument("--brightness", type=float, default=1.0, help="Pillow brightness factor")
    print_cmd.add_argument("--contrast", type=float, default=1.0, help="Pillow contrast factor")
    print_cmd.add_argument("--font-size", type=int, default=28, help="point size for --text")
    print_cmd.add_argument("--font", help="TTF path for --text")
    print_cmd.add_argument("--pace", type=float, default=0.015, help="seconds between two-row Bluetooth writes")
    print_cmd.set_defaults(handler=_cmd_print)

    return parser


def _add_target(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("address", help="Bluetooth MAC, e.g. AA:BB:CC:DD:EE:FF")
    parser.add_argument(
        "--dump",
        metavar="FILE",
        help="write protocol bytes to FILE instead of opening Bluetooth (use - for stdout)",
    )
    parser.add_argument("--timeout", type=float, default=20.0, help="Bluetooth connect/write timeout in seconds")
    parser.add_argument(
        "--transport",
        choices=("ble", "bleak", "rfcomm"),
        default="ble",
        help="ble = gatttool LE (default); bleak = BlueZ GATT via bleak; rfcomm = classic SPP",
    )


def _cmd_discover(args: argparse.Namespace) -> int:
    from .discover import discover

    devices = discover(scan_s=args.scan)
    if not devices:
        print("no Bluetooth devices found", file=sys.stderr)
        return 1
    for device in devices:
        mark = "*" if device.likely_peripage else " "
        print(f"{mark} {device.address}  {device.name}")
    return 0


def _cmd_info(args: argparse.Namespace) -> int:
    with _printer(args) as printer:
        info = printer.info()
    print(f"name      {info.name}")
    print(f"firmware  {info.firmware}")
    print(f"serial    {info.serial}")
    print(f"hardware  {info.hardware}")
    print(f"model     {info.model_id}")
    print(f"mac       {info.mac}")
    print(f"battery   {info.battery if info.battery is not None else 'n/a'}%")
    return 0


def _cmd_feed(args: argparse.Namespace) -> int:
    with _printer(args) as printer:
        printer.feed(args.dots)
    return 0


def _cmd_print(args: argparse.Namespace) -> int:
    concentration = _parse_concentration(args.concentration)
    with _printer(args, pace_s=args.pace) as printer:
        if args.image:
            path = Path(args.image)
            if not path.is_file():
                raise FileNotFoundError(path)
            printer.print_image(
                str(path),
                dither=Dither(args.dither),
                brightness=args.brightness,
                contrast=args.contrast,
                rotate=args.rotate,
                concentration=concentration,
                feed_dots=args.feed,
            )
        elif args.text is not None:
            printer.print_text(
                args.text,
                font_path=args.font,
                font_size=args.font_size,
                concentration=concentration,
                feed_dots=args.feed,
            )
        elif args.ascii is not None:
            printer.print_ascii(args.ascii, feed_dots=args.feed)
        elif args.qr is not None:
            printer.print_qr(args.qr, concentration=concentration, feed_dots=args.feed)
    return 0


def _printer(args: argparse.Namespace, *, pace_s: float = 0.015) -> Printer:
    dump = getattr(args, "dump", None)
    if dump is not None:
        dest = sys.stdout.buffer if dump == "-" else dump
        transport = DumpTransport(dest)
        return Printer(transport, pace_s=0.0, chunk_pause_s=0.0, settle_s=0.0)
    address = normalize_address(args.address)
    timeout = getattr(args, "timeout", 20.0)
    kind = getattr(args, "transport", "ble")
    if kind == "rfcomm":
        transport = BluetoothTransport(address, timeout=timeout)
    elif kind == "bleak":
        transport = BleTransport(address, timeout=timeout)
    else:
        transport = open_ble_transport(address, timeout=timeout)
    return Printer(transport, pace_s=pace_s)


def _parse_concentration(value: str) -> Concentration:
    names = {
        "light": Concentration.LIGHT,
        "medium": Concentration.MEDIUM,
        "dark": Concentration.DARK,
        "0": Concentration.LIGHT,
        "1": Concentration.MEDIUM,
        "2": Concentration.DARK,
    }
    return names[value]


if __name__ == "__main__":
    sys.exit(main())
