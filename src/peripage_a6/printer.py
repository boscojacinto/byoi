"""High-level PeriPage A6 304dpi printer."""

from __future__ import annotations

import time
from dataclasses import dataclass

from PIL import Image

from .models import A6_304, Model
from .protocol import (
    CMD_QUERY_BATTERY,
    CMD_QUERY_FIRMWARE,
    CMD_QUERY_HARDWARE,
    CMD_QUERY_MAC,
    CMD_QUERY_MODEL,
    CMD_QUERY_NAME,
    CMD_QUERY_SERIAL,
    SAFE_CHUNK_ROWS,
    Concentration,
    ascii_line,
    decode_ascii,
    filter_ascii,
    decode_battery,
    decode_mac,
    end_job,
    feed as feed_cmd,
    reset as reset_cmd,
    set_concentration as concentration_cmd,
    iter_raster_packets,
)
from .raster import Dither, image_to_raster, render_text
from .transport import Transport, TransportError


class PrinterError(RuntimeError):
    pass


@dataclass(frozen=True)
class DeviceInfo:
    name: str
    firmware: str
    serial: str
    hardware: str
    model_id: str
    mac: str
    battery: int | None


class Printer:
    """Talk to one PeriPage A6 304dpi over an already-constructed transport.

    Call :meth:`connect` (or use the context manager) before printing. The
    firmware ignores raster and query commands until :meth:`reset` runs; that
    happens automatically on connect.
    """

    def __init__(
        self,
        transport: Transport,
        *,
        model: Model = A6_304,
        chunk_rows: int = SAFE_CHUNK_ROWS,
        pace_s: float = 0.015,
        chunk_pause_s: float = 0.05,
        settle_s: float = 0.05,
    ) -> None:
        self.transport = transport
        self.model = model
        self.chunk_rows = chunk_rows
        self.pace_s = pace_s
        self.chunk_pause_s = chunk_pause_s
        self.settle_s = settle_s
        self._connected = False

    def connect(self) -> None:
        self.transport.open()
        self._connected = True
        self.reset()

    def disconnect(self) -> None:
        self._connected = False
        self.transport.close()

    def __enter__(self) -> "Printer":
        self.connect()
        return self

    def __exit__(self, *exc: object) -> None:
        self.disconnect()

    def reset(self) -> None:
        self._write(reset_cmd())
        if self.settle_s:
            time.sleep(self.settle_s)

    def set_concentration(self, level: int | Concentration) -> None:
        self._write(concentration_cmd(level))

    def feed(self, dots: int = 64) -> None:
        remaining = max(0, int(dots))
        while remaining > 0:
            n = min(255, remaining)
            self._write(feed_cmd(n))
            remaining -= n

    def finish(self, feed_dots: int = 64) -> None:
        if feed_dots:
            self.feed(feed_dots)
        self._write(end_job())

    def info(self) -> DeviceInfo:
        battery: int | None
        try:
            battery = decode_battery(self._ask(CMD_QUERY_BATTERY))
        except (PrinterError, ValueError):
            battery = None
        return DeviceInfo(
            name=decode_ascii(self._ask(CMD_QUERY_NAME)),
            firmware=decode_ascii(self._ask(CMD_QUERY_FIRMWARE)),
            serial=decode_ascii(self._ask(CMD_QUERY_SERIAL)),
            hardware=decode_ascii(self._ask(CMD_QUERY_HARDWARE)),
            model_id=decode_ascii(self._ask(CMD_QUERY_MODEL)),
            mac=decode_mac(self._ask(CMD_QUERY_MAC)),
            battery=battery,
        )

    def battery(self) -> int:
        return decode_battery(self._ask(CMD_QUERY_BATTERY))

    def print_raster(
        self,
        payload: bytes,
        *,
        concentration: int | Concentration | None = None,
        feed_dots: int = 64,
    ) -> None:
        if not payload:
            return
        for i, (header, rows) in enumerate(
            iter_raster_packets(payload, self.chunk_rows, self.model)
        ):
            if i and self.chunk_pause_s:
                time.sleep(self.chunk_pause_s)
            self.reset()
            if concentration is not None:
                self.set_concentration(concentration)
            self._write(header)
            for row in rows:
                self._write(row)
                if self.pace_s:
                    time.sleep(self.pace_s)
        self.finish(feed_dots)

    def print_image(
        self,
        image: Image.Image | str,
        *,
        dither: Dither = Dither.FLOYD_STEINBERG,
        brightness: float = 1.0,
        contrast: float = 1.0,
        rotate: int = 0,
        concentration: int | Concentration | None = Concentration.MEDIUM,
        feed_dots: int = 64,
        threshold: int = 128,
    ) -> None:
        if isinstance(image, str):
            image = Image.open(image)
        payload = image_to_raster(
            image,
            model=self.model,
            dither=dither,
            brightness=brightness,
            contrast=contrast,
            rotate=rotate,
            threshold=threshold,
        )
        self.print_raster(payload, concentration=concentration, feed_dots=feed_dots)

    def print_text(
        self,
        text: str,
        *,
        font_path: str | None = None,
        font_size: int = 28,
        dither: Dither = Dither.THRESHOLD,
        concentration: int | Concentration | None = Concentration.MEDIUM,
        feed_dots: int = 64,
    ) -> None:
        image = render_text(
            text,
            model=self.model,
            font_path=font_path,
            font_size=font_size,
        )
        self.print_image(
            image,
            dither=dither,
            concentration=concentration,
            feed_dots=feed_dots,
        )

    def print_ascii(
        self,
        text: str,
        *,
        feed_dots: int = 64,
        line_delay_s: float = 0.2,
    ) -> None:
        """Use the printer's built-in 7-bit font (48 columns). Prefer print_text()."""
        width = self.model.ascii_columns
        for raw_line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
            cleaned = filter_ascii(raw_line)
            if not cleaned:
                self.feed(30)
                if line_delay_s:
                    time.sleep(line_delay_s)
                continue
            for i in range(0, len(cleaned), width):
                self._write(ascii_line(cleaned[i : i + width], self.model))
                if line_delay_s:
                    time.sleep(line_delay_s)
        self.finish(feed_dots)

    def print_qr(
        self,
        data: str,
        *,
        concentration: int | Concentration | None = Concentration.MEDIUM,
        feed_dots: int = 64,
        border: int = 2,
    ) -> None:
        import qrcode

        qr = qrcode.QRCode(border=border, box_size=4, error_correction=qrcode.constants.ERROR_CORRECT_M)
        qr.add_data(data)
        qr.make(fit=True)
        image = qr.make_image(fill_color="black", back_color="white").convert("RGB")
        self.print_image(
            image,
            dither=Dither.THRESHOLD,
            concentration=concentration,
            feed_dots=feed_dots,
        )

    def _write(self, data: bytes) -> None:
        if not self._connected:
            raise PrinterError("printer is not connected")
        if not data:
            return
        self.transport.write(data)

    def _ask(self, command: bytes, size: int = 1024) -> bytes:
        self._write(command)
        try:
            reply = self.transport.read(size)
        except TransportError as exc:
            raise PrinterError(f"no reply to {command.hex()}: {exc}") from exc
        if not reply:
            raise PrinterError(f"no reply to {command.hex()}")
        return reply
