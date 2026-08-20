"""Find PeriPage printers via BlueZ and a BLE inquiry."""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass

from .transport import normalize_address


@dataclass(frozen=True)
class DiscoveredDevice:
    address: str
    name: str

    @property
    def likely_peripage(self) -> bool:
        return "peripage" in self.name.lower()


def discover(scan_s: float = 5.0) -> list[DiscoveredDevice]:
    """Return nearby and already-known Bluetooth devices.

    BLE PeriPage units show up as ``PeriPage+XXXX_BLE``. Classic pairing is
    not required; a GATT connect is enough.
    """
    found: dict[str, DiscoveredDevice] = {}
    bluetoothctl = shutil.which("bluetoothctl")
    if bluetoothctl is not None:
        try:
            for device in _parse_devices(_run(bluetoothctl, ["devices"])):
                found[device.address] = device
        except RuntimeError:
            pass
    if scan_s > 0:
        for device in _ble_scan(scan_s):
            found[device.address] = device
    return list(found.values())


def _ble_scan(scan_s: float) -> list[DiscoveredDevice]:
    try:
        from bleak import BleakScanner
    except ImportError as exc:
        raise RuntimeError("BLE scan requires bleak: pip install bleak") from exc

    import asyncio

    async def _run_scan() -> list[DiscoveredDevice]:
        devices = await BleakScanner.discover(timeout=scan_s)
        out: list[DiscoveredDevice] = []
        for device in devices:
            name = device.name or ""
            try:
                address = normalize_address(device.address)
            except ValueError:
                continue
            out.append(DiscoveredDevice(address=address, name=name))
        return out

    return asyncio.run(_run_scan())


def _run(bluetoothctl: str, args: list[str]) -> str:
    result = subprocess.run(
        [bluetoothctl, *args],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"bluetoothctl {' '.join(args)} failed: {err}")
    return result.stdout


def _parse_devices(output: str) -> list[DiscoveredDevice]:
    found: list[DiscoveredDevice] = []
    seen: set[str] = set()
    for line in output.splitlines():
        line = line.strip()
        if not line.startswith("Device "):
            continue
        parts = line.split(maxsplit=2)
        if len(parts) < 2:
            continue
        try:
            address = normalize_address(parts[1])
        except ValueError:
            continue
        if address in seen:
            continue
        seen.add(address)
        name = parts[2] if len(parts) > 2 else ""
        found.append(DiscoveredDevice(address=address, name=name))
    return found
