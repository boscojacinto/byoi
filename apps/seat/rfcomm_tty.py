"""RFCOMM serial TTY: no IP. Phone SPP <-> seat tmux/claude/shell."""

from __future__ import annotations

import os
import select
import socket
import subprocess
import threading
import time

from .tmux_claude import attach_argv, ensure_session

SPP_UUID = "00001101-0000-1000-8000-00805f9b34fb"
CHANNEL = int(os.environ.get("BYOI_RFCOMM_CHANNEL", "1"))
PROFILE_PATH = "/org/byoi/rfcomm"


def parse_adapter_list(text: str) -> str | None:
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == "Controller":
            return parts[1].upper()
    return None


def adapter_address() -> str:
    try:
        out = subprocess.check_output(["bluetoothctl", "list"], text=True, timeout=3)
        parsed = parse_adapter_list(out)
        if parsed:
            return parsed
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        pass
    return "00:00:00:00:00:00"


def _splice(conn: socket.socket) -> None:
    import pty as pty_mod

    ensure_session()
    argv = attach_argv()
    pid, fd = pty_mod.fork()
    if pid == 0:
        os.execvp(argv[0], argv)
        os._exit(1)
    os.set_blocking(fd, False)
    conn.setblocking(False)
    try:
        while True:
            readable, _, _ = select.select([conn, fd], [], [], 30)
            if not readable:
                continue
            if conn in readable:
                try:
                    chunk = conn.recv(4096)
                except BlockingIOError:
                    chunk = b""
                if not chunk:
                    break
                os.write(fd, chunk)
            if fd in readable:
                try:
                    chunk = os.read(fd, 4096)
                except BlockingIOError:
                    chunk = b""
                if not chunk:
                    break
                conn.sendall(chunk)
    except OSError:
        pass
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.kill(pid, 15)
        except OSError:
            pass
        try:
            conn.close()
        except OSError:
            pass


class RfcommServer(threading.Thread):
    def __init__(self, channel: int = CHANNEL) -> None:
        super().__init__(name="byoi-rfcomm", daemon=True)
        self.channel = channel
        self.address = adapter_address()
        self.error: str | None = None
        self.listening = False
        self.clients = 0
        self.via: str | None = None
        self._sock: socket.socket | None = None
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass

    def snapshot(self) -> dict:
        return {
            "transport": "rfcomm",
            "uuid": SPP_UUID,
            "channel": self.channel,
            "adapter": self.address,
            "listening": self.listening,
            "clients": self.clients,
            "via": self.via,
            "error": self.error,
        }

    def run(self) -> None:
        if not hasattr(socket, "AF_BLUETOOTH"):
            self.error = "no AF_BLUETOOTH"
            return
        subprocess.run(
            ["bluetoothctl", "power", "on"],
            check=False,
            capture_output=True,
        )
        subprocess.run(["bluetoothctl", "pairable", "on"], check=False, capture_output=True)
        subprocess.run(["bluetoothctl", "discoverable", "on"], check=False, capture_output=True)
        if self._listen_bind():
            self._accept_loop()
            return
        self.error = self.error or "RFCOMM bind failed"

    def _listen_bind(self) -> bool:
        if self.address == "00:00:00:00:00:00":
            self.error = "no bluetooth adapter MAC"
            return False
        sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM)
        try:
            sock.bind((self.address, self.channel))
            sock.listen(1)
            sock.settimeout(1.0)
        except OSError as exc:
            self.error = str(exc)
            sock.close()
            return False
        self._sock = sock
        self.listening = True
        self.via = "socket"
        self.error = None
        return True

    def _accept_loop(self) -> None:
        sock = self._sock
        assert sock is not None
        while not self._stop.is_set():
            try:
                conn, _peer = sock.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            self.clients += 1
            try:
                _splice(conn)
            finally:
                self.clients = max(0, self.clients - 1)
        self.listening = False


_server: RfcommServer | None = None


def start_rfcomm() -> RfcommServer:
    global _server
    if _server and _server.is_alive():
        return _server
    _server = RfcommServer()
    _server.start()
    time.sleep(0.2)
    return _server


def rfcomm_status() -> dict:
    if _server is None:
        return {
            "transport": "rfcomm",
            "uuid": SPP_UUID,
            "channel": CHANNEL,
            "listening": False,
        }
    return _server.snapshot()
