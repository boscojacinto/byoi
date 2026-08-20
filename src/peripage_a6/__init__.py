"""Userspace driver for the PeriPage A6 304dpi thermal printer."""

from .models import A6_304, Model
from .printer import DeviceInfo, Printer, PrinterError
from .protocol import Concentration
from .raster import Dither
from .transport import (
    BleTransport,
    BluetoothTransport,
    DumpTransport,
    GattToolTransport,
    Transport,
    open_ble_transport,
)

__version__ = "0.1.0"

__all__ = [
    "A6_304",
    "BleTransport",
    "BluetoothTransport",
    "GattToolTransport",
    "Concentration",
    "DeviceInfo",
    "Dither",
    "DumpTransport",
    "Model",
    "Printer",
    "PrinterError",
    "Transport",
    "open_ble_transport",
    "__version__",
]
