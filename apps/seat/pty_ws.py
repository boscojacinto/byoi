"""PTY websocket that attaches to the seat tmux session."""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import pty
import struct
import termios

from fastapi import WebSocket, WebSocketDisconnect

from apps.secrets import scrub

from .tmux_claude import attach_argv, ensure_session


def _set_winsize(fd: int, rows: int, cols: int) -> None:
    packed = struct.pack("HHHH", max(1, rows), max(2, cols), 0, 0)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, packed)


async def attach_tmux(ws: WebSocket) -> None:
    info = ensure_session()
    if not info.get("tmux"):
        await ws.send_text(f"\r\n[seat] {info.get('error') or 'tmux unavailable'}\r\n")
        await ws.close()
        return

    pid, fd = pty.fork()
    if pid == 0:
        argv = attach_argv()
        os.execvpe(argv[0], argv, scrub(os.environ.copy()))
        os._exit(1)

    os.set_blocking(fd, False)

    async def pump_pty() -> None:
        try:
            while True:
                try:
                    data = os.read(fd, 4096)
                except BlockingIOError:
                    await asyncio.sleep(0.02)
                    continue
                if not data:
                    break
                await ws.send_bytes(data)
        except (WebSocketDisconnect, OSError):
            pass

    reader = asyncio.create_task(pump_pty())
    try:
        while True:
            message = await ws.receive()
            if message.get("type") == "websocket.disconnect":
                break
            raw = message.get("bytes") or message.get("text")
            if raw is None:
                continue
            if isinstance(raw, str) and raw.startswith("{") and '"cols"' in raw:
                try:
                    geo = json.loads(raw)
                    _set_winsize(fd, int(geo.get("rows", 24)), int(geo.get("cols", 80)))
                except (ValueError, KeyError, json.JSONDecodeError):
                    os.write(fd, raw.encode())
                continue
            os.write(fd, raw if isinstance(raw, (bytes, bytearray)) else raw.encode())
    except WebSocketDisconnect:
        pass
    finally:
        reader.cancel()
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.kill(pid, 15)
        except OSError:
            pass
