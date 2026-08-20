#!/usr/bin/env python3
"""Minimal library example. Pass the printer MAC as argv[1]."""

import sys

from peripage_a6 import Concentration, Printer, open_ble_transport


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit(f"usage: {sys.argv[0]} AA:BB:CC:DD:EE:FF")
    with Printer(open_ble_transport(sys.argv[1])) as printer:
        info = printer.info()
        print(f"{info.name}  fw={info.firmware}  battery={info.battery}%")
        printer.print_text("PeriPage A6 304dpi\nhello from byoi", concentration=Concentration.MEDIUM)


if __name__ == "__main__":
    main()
