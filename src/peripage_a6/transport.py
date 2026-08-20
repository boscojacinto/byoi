"""Byte pipes the printer talks over.

The 304dpi A6 sold as ``PeriPage+XXXX_BLE`` is a GATT device. Classic RFCOMM
times out even though it advertises Serial Port. Use :class:`BleTransport`.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import os
import pty
import queue
import re
import select
import shutil
import socket
import subprocess
import threading
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import BinaryIO


RFCOMM_CHANNEL = 1

# GATT profile observed on PeriPage+B250_BLE / PeriPage+8B91_BLE
BLE_SERVICE_FF00 = "0000ff00-0000-1000-8000-00805f9b34fb"
BLE_WRITE_UUID = "0000ff02-0000-1000-8000-00805f9b34fb"
BLE_NOTIFY_UUID = "0000ff01-0000-1000-8000-00805f9b34fb"
ISSC_SERVICE = "49535343-fe7d-4ae5-8fa9-9fafd205e455"
ISSC_WRITE_UUID = "49535343-8841-43f4-a8d4-ecbe34729bb3"
ISSC_NOTIFY_UUID = "49535343-1e4d-4bd9-ba61-23c647249616"


class TransportError(RuntimeError):
    pass


class Transport(ABC):
    @abstractmethod
    def open(self) -> None: ...

    @abstractmethod
    def close(self) -> None: ...

    @abstractmethod
    def write(self, data: bytes) -> None: ...

    @abstractmethod
    def read(self, size: int = 1024) -> bytes: ...

    def __enter__(self) -> "Transport":
        self.open()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class BluetoothTransport(Transport):
    """Classic Bluetooth SPP via RFCOMM. Linux stdlib sockets, no PyBluez.

    The printer must already be paired::

        bluetoothctl pair AA:BB:CC:DD:EE:FF
        bluetoothctl trust AA:BB:CC:DD:EE:FF
    """

    def __init__(
        self,
        address: str,
        *,
        channel: int = RFCOMM_CHANNEL,
        timeout: float = 5.0,
    ) -> None:
        self.address = normalize_address(address)
        self.channel = channel
        self.timeout = timeout
        self._sock: socket.socket | None = None

    def open(self) -> None:
        if not hasattr(socket, "AF_BLUETOOTH"):
            raise TransportError(
                "This Python build has no AF_BLUETOOTH. Linux is required for "
                "the stdlib RFCOMM backend."
            )
        sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM)
        sock.settimeout(self.timeout)
        try:
            sock.connect((self.address, self.channel))
        except OSError as exc:
            sock.close()
            raise TransportError(
                f"could not connect to {self.address} on RFCOMM channel "
                f"{self.channel}: {exc}. Is the printer on and paired?"
            ) from exc
        self._sock = sock

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None

    def write(self, data: bytes) -> None:
        sock = self._require()
        view = memoryview(data)
        sent = 0
        while sent < len(view):
            n = sock.send(view[sent:])
            if n == 0:
                raise TransportError("Bluetooth socket closed while sending")
            sent += n

    def read(self, size: int = 1024) -> bytes:
        sock = self._require()
        try:
            return sock.recv(size)
        except TimeoutError as exc:
            raise TransportError("timed out waiting for printer response") from exc
        except OSError as exc:
            raise TransportError(f"Bluetooth read failed: {exc}") from exc

    def _require(self) -> socket.socket:
        if self._sock is None:
            raise TransportError("transport is closed")
        return self._sock


_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_CHAR_LINE = re.compile(
    r"char value handle:\s*0x([0-9a-fA-F]+).*?uuid:\s*([0-9a-fA-F-]+)",
    re.IGNORECASE,
)
_NOTIFY_LINE = re.compile(
    r"Notification handle = 0x([0-9a-fA-F]+)\s+value:\s*([0-9a-fA-F ]+)",
    re.IGNORECASE,
)


def parse_gatt_characteristics(text: str) -> dict[str, int]:
    """Map characteristic UUID -> value handle from ``gatttool characteristics``."""
    found: dict[str, int] = {}
    for match in _CHAR_LINE.finditer(_ANSI.sub("", text)):
        found[match.group(2).lower()] = int(match.group(1), 16)
    return found


def open_ble_transport(address: str, *, timeout: float = 20.0) -> Transport:
    """Prefer gatttool LE. BlueZ Device.Connect() on this printer tries BR/EDR and fails."""
    if shutil.which("gatttool"):
        return GattToolTransport(address, timeout=timeout)
    return BleTransport(address, timeout=timeout)


class GattToolTransport(Transport):
    """Linux LE transport via a persistent ``gatttool -I`` session.

    BlueZ ``Connect()`` on dual-mode PeriPage units tries the advertised Serial
    Port profile (BR/EDR) and returns ``br-connection-profile-unavailable``.
    ``gatttool`` opens an LE ATT link instead. Do not ``bluetoothctl pair``.
    """

    def __init__(self, address: str, *, timeout: float = 20.0) -> None:
        self.address = normalize_address(address)
        self.timeout = timeout
        self.write_handle = 0x0012  # 0000ff02 value handle on A6 304dpi
        self.notify_handle = 0x000F  # 0000ff01 value handle
        self._proc: subprocess.Popen[bytes] | None = None
        self._master: int | None = None
        self._lock = threading.Lock()
        self._notify: queue.Queue[bytes] = queue.Queue()

    def open(self) -> None:
        if shutil.which("gatttool") is None:
            raise TransportError("gatttool not found. Install BlueZ (package bluez).")
        master, slave = pty.openpty()
        try:
            proc = subprocess.Popen(
                ["gatttool", "-b", self.address, "-t", "public", "-I"],
                stdin=slave,
                stdout=slave,
                stderr=slave,
                close_fds=True,
            )
        except OSError as exc:
            os.close(master)
            os.close(slave)
            raise TransportError(f"could not start gatttool: {exc}") from exc
        os.close(slave)
        self._master = master
        self._proc = proc
        try:
            self._drain(0.3)
            self._connect_le()
            self._cmd("mtu 200", wait=1.0)
            self._discover_handles()
            # CCCD sits immediately after the notify value handle.
            self._cmd(f"char-write-req 0x{self.notify_handle + 1:04x} 0100", wait=1.0)
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        with self._lock:
            if self._master is not None:
                try:
                    os.write(self._master, b"disconnect\n")
                    time.sleep(0.2)
                except OSError:
                    pass
            if self._proc is not None:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
                self._proc = None
            if self._master is not None:
                try:
                    os.close(self._master)
                except OSError:
                    pass
                self._master = None

    def write(self, data: bytes) -> None:
        self._cmd(f"char-write-cmd 0x{self.write_handle:04x} {data.hex()}", wait=0.15)

    def read(self, size: int = 1024) -> bytes:
        deadline = time.time() + self.timeout
        collected = bytearray()
        while time.time() < deadline:
            remaining = max(0.05, deadline - time.time())
            text = self._drain(remaining)
            self._ingest_notifications(text)
            try:
                while True:
                    collected.extend(self._notify.get_nowait())
            except queue.Empty:
                pass
            if collected:
                break
        if not collected:
            raise TransportError("timed out waiting for printer notification")
        return bytes(collected[:size] if size < len(collected) else collected)

    def _connect_le(self) -> None:
        last = ""
        for attempt in range(4):
            last = self._cmd("connect", wait=min(8.0, self.timeout))
            if re.search(r"connection successful", last, re.IGNORECASE):
                return
            time.sleep(1.0 + attempt)
        raise TransportError(
            f"gatttool could not connect to {self.address}: {last.strip() or 'no reply'}. "
            "Power the printer on (green LED), keep it off the phone, and do not bluetoothctl pair."
        )

    def _discover_handles(self) -> None:
        text = self._cmd("characteristics", wait=3.0)
        found = parse_gatt_characteristics(text)
        if BLE_WRITE_UUID in found:
            self.write_handle = found[BLE_WRITE_UUID]
        if BLE_NOTIFY_UUID in found:
            self.notify_handle = found[BLE_NOTIFY_UUID]
        elif ISSC_WRITE_UUID in found:
            self.write_handle = found[ISSC_WRITE_UUID]
            self.notify_handle = found.get(ISSC_NOTIFY_UUID, self.notify_handle)

    def _cmd(self, command: str, *, wait: float) -> str:
        with self._lock:
            if self._master is None:
                raise TransportError("transport is closed")
            os.write(self._master, (command + "\n").encode())
            return self._drain(wait)

    def _drain(self, timeout: float) -> str:
        if self._master is None:
            return ""
        buf = bytearray()
        end = time.time() + timeout
        while time.time() < end:
            remaining = max(0.0, end - time.time())
            ready, _, _ = select.select([self._master], [], [], remaining)
            if not ready:
                if buf:
                    extra = time.time() + 0.12
                    while time.time() < extra:
                        ready, _, _ = select.select([self._master], [], [], extra - time.time())
                        if not ready:
                            break
                        buf.extend(os.read(self._master, 4096))
                break
            chunk = os.read(self._master, 4096)
            if not chunk:
                break
            buf.extend(chunk)
        text = _ANSI.sub("", buf.decode("utf-8", errors="replace"))
        self._ingest_notifications(text)
        if "Command Failed" in text:
            raise TransportError(text.strip())
        return text

    def _ingest_notifications(self, text: str) -> None:
        for match in _NOTIFY_LINE.finditer(text):
            handle = int(match.group(1), 16)
            if handle != self.notify_handle:
                continue
            payload = bytes(int(part, 16) for part in match.group(2).split() if part)
            if payload:
                self._notify.put(payload)


_BLE_LOOP: asyncio.AbstractEventLoop | None = None
_BLE_LOOP_THREAD: threading.Thread | None = None
_BLE_LOOP_LOCK = threading.Lock()


def _shared_ble_loop() -> asyncio.AbstractEventLoop:
    """One process-wide loop. Bleak's BlueZ manager dies if we tear the loop down."""
    global _BLE_LOOP, _BLE_LOOP_THREAD
    with _BLE_LOOP_LOCK:
        if _BLE_LOOP is not None and _BLE_LOOP.is_running():
            return _BLE_LOOP
        loop = asyncio.new_event_loop()

        def _run() -> None:
            asyncio.set_event_loop(loop)
            loop.run_forever()

        thread = threading.Thread(target=_run, name="peripage-ble", daemon=True)
        thread.start()
        _BLE_LOOP = loop
        _BLE_LOOP_THREAD = thread
        return loop


class BleTransport(Transport):
    """GATT transport for PeriPage A6 304dpi BLE models (``PeriPage+XXXX_BLE``).

    Writes go to ``0000ff02``; replies arrive as notifications on ``0000ff01``.
    Classic ``bluetoothctl pair`` often returns AuthenticationFailed on these
    devices — connect is enough. Pairing is Just Works / optional.
    """

    def __init__(self, address: str, *, timeout: float = 20.0) -> None:
        self.address = normalize_address(address)
        self.timeout = timeout
        self.write_uuid = BLE_WRITE_UUID
        self.notify_uuid = BLE_NOTIFY_UUID
        self._loop: asyncio.AbstractEventLoop | None = None
        self._client = None
        self._notify: queue.Queue[bytes] = queue.Queue()

    def open(self) -> None:
        try:
            import bleak  # noqa: F401
        except ImportError as exc:
            raise TransportError("BLE transport requires the bleak package: pip install bleak") from exc
        self._loop = _shared_ble_loop()
        try:
            self._call(self._connect(), timeout=max(self.timeout, 45.0))
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        client = self._client
        self._client = None
        if client is not None and self._loop is not None:
            try:
                self._call(client.disconnect(), timeout=5.0)
            except Exception:
                pass

    def write(self, data: bytes) -> None:
        client = self._client
        if client is None:
            raise TransportError("transport is closed")
        try:
            self._call(
                client.write_gatt_char(self.write_uuid, data, response=False),
                timeout=self.timeout,
            )
        except Exception as exc:
            raise TransportError(f"BLE write failed: {exc}") from exc

    def read(self, size: int = 1024) -> bytes:
        try:
            data = self._notify.get(timeout=self.timeout)
        except queue.Empty:
            raise TransportError("timed out waiting for printer notification") from None
        extra = bytes(data)
        while len(extra) < size:
            try:
                extra += self._notify.get_nowait()
            except queue.Empty:
                break
        return extra[:size] if size < len(extra) else extra

    def _call(self, coro, timeout: float | None = None):
        if self._loop is None:
            raise TransportError("transport is closed")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return future.result(timeout if timeout is not None else self.timeout)
        except concurrent.futures.TimeoutError as exc:
            future.cancel()
            raise TransportError("BLE operation timed out") from exc
        except Exception as exc:
            raise TransportError(str(exc)) from exc

    async def _connect(self) -> None:
        from bleak import BleakClient
        from bleak.exc import BleakError

        last_error: Exception | None = None
        for attempt in range(4):
            target = await self._resolve_device()
            path = target.details.get("path") if hasattr(target, "details") else None
            if path:
                await self._nudge_bluez_connect(path)
            client = BleakClient(target, timeout=self.timeout)
            try:
                await client.connect()
                char_uuids = {
                    c.uuid.lower()
                    for service in client.services
                    for c in service.characteristics
                }
                if BLE_WRITE_UUID not in char_uuids and ISSC_WRITE_UUID in char_uuids:
                    self.write_uuid = ISSC_WRITE_UUID
                    self.notify_uuid = ISSC_NOTIFY_UUID
                try:
                    await client.start_notify(self.notify_uuid, self._on_notify)
                except Exception:
                    pass
                self._client = client
                return
            except (BleakError, TransportError) as exc:
                last_error = exc
                transient = "profile-unavailable" in str(exc) or "le-connection-abort" in str(exc)
                if not transient:
                    try:
                        await client.disconnect()
                    except Exception:
                        pass
                await asyncio.sleep(0.8 * (attempt + 1))
        raise TransportError(
            f"could not connect to {self.address} over BLE: {last_error}. "
            "Is the printer on, within range, and disconnected from a phone?"
        ) from last_error

    async def _nudge_bluez_connect(self, path: str) -> None:
        """Bring GATT up without Bleak's Connect() cleanup.

        Dual-mode PeriPage firmware advertises Serial Port. BlueZ ``Connect()``
        then returns ``br-connection-profile-unavailable`` after GATT is already
        connected. Bleak treats that as fatal and disconnects. We ignore the
        RFCOMM failure and wait for ``ServicesResolved``.
        """
        from dbus_fast.aio import MessageBus
        from dbus_fast.constants import BusType, MessageType
        from dbus_fast.message import Message

        from bleak.backends.bluezdbus.manager import get_global_bluez_manager

        manager = await get_global_bluez_manager()
        if manager.is_connected(path):
            props = manager._properties.get(path, {}).get("org.bluez.Device1", {})
            if props.get("ServicesResolved"):
                return

        bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        try:
            reply = await bus.call(
                Message(
                    destination="org.bluez",
                    path=path,
                    interface="org.bluez.Device1",
                    member="Connect",
                )
            )
            if reply is not None and reply.message_type == MessageType.ERROR:
                body = " ".join(str(x) for x in (reply.body or []))
                if "profile-unavailable" not in body and "profile-unavailable" not in (reply.error_name or ""):
                    # Other errors still often leave GATT up; only bail if we
                    # never see Connected below.
                    pass
        finally:
            bus.disconnect()

        for _ in range(25):
            if manager.is_connected(path):
                props = manager._properties.get(path, {}).get("org.bluez.Device1", {})
                if props.get("ServicesResolved"):
                    return
            await asyncio.sleep(0.2)

    async def _resolve_device(self):
        """Skip BLE scan when BlueZ already knows the printer (it is not advertising)."""
        from bleak import BleakScanner
        from bleak.backends.device import BLEDevice

        try:
            from bleak.backends.bluezdbus.manager import get_global_bluez_manager

            manager = await get_global_bluez_manager()
            adapter = manager.get_default_adapter()
            path = f"{adapter}/dev_{self.address.replace(':', '_')}"
            props = manager._properties.get(path, {}).get("org.bluez.Device1")
            if props is not None:
                return BLEDevice(self.address, props.get("Name"), {"path": path, "props": props})
        except Exception:
            pass
        device = await BleakScanner.find_device_by_address(self.address, timeout=self.timeout)
        if device is None:
            raise TransportError(
                f"BLE device {self.address} not found. Power the printer on and keep it "
                "disconnected from a phone."
            )
        return device

    def _on_notify(self, _sender, data: bytearray) -> None:
        self._notify.put(bytes(data))


class DumpTransport(Transport):
    """Write-only sink used for dry-runs and protocol tests."""

    def __init__(self, dest: str | Path | BinaryIO | None = None) -> None:
        self._path = Path(dest) if isinstance(dest, (str, Path)) else None
        self._external = dest if not isinstance(dest, (str, Path)) and dest is not None else None
        self._fh: BinaryIO | None = None
        self.buffer = bytearray()
        self.writes: list[bytes] = []

    def open(self) -> None:
        if self._path is not None:
            self._fh = self._path.open("wb")
        elif self._external is not None:
            self._fh = self._external

    def close(self) -> None:
        if self._path is not None and self._fh is not None:
            self._fh.close()
        self._fh = None

    def write(self, data: bytes) -> None:
        self.writes.append(bytes(data))
        self.buffer.extend(data)
        if self._fh is not None:
            self._fh.write(data)
            self._fh.flush()

    def read(self, size: int = 1024) -> bytes:
        return b""


class DummyTransport(Transport):
    """In-memory transport with scripted query replies."""

    def __init__(self, replies: dict[bytes, bytes] | None = None) -> None:
        self.replies = dict(replies or {})
        self.writes: list[bytes] = []
        self._pending = bytearray()
        self.opened = False

    def open(self) -> None:
        self.opened = True

    def close(self) -> None:
        self.opened = False

    def write(self, data: bytes) -> None:
        blob = bytes(data)
        self.writes.append(blob)
        reply = self.replies.get(blob)
        if reply:
            self._pending.extend(reply)

    def read(self, size: int = 1024) -> bytes:
        out = bytes(self._pending[:size])
        del self._pending[:size]
        return out

    @property
    def sent(self) -> bytes:
        return b"".join(self.writes)


def normalize_address(address: str) -> str:
    cleaned = address.strip().replace("-", ":").upper()
    parts = cleaned.split(":")
    if len(parts) != 6 or any(len(p) != 2 for p in parts):
        raise ValueError(f"invalid Bluetooth address: {address!r}")
    try:
        for part in parts:
            int(part, 16)
    except ValueError as exc:
        raise ValueError(f"invalid Bluetooth address: {address!r}") from exc
    return ":".join(parts)
