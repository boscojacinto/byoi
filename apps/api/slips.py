"""Compose 576px thermal slips for the PeriPage A6."""

from __future__ import annotations

import os
import socket
from io import BytesIO
from pathlib import Path
from urllib.parse import urlparse

import qrcode
from PIL import Image, ImageDraw

from peripage_a6.models import A6_304
from peripage_a6.raster import render_text

WIFI_SSID = os.environ.get("BYOI_WIFI_SSID", "salon Wi-Fi")
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}

# Widest the QR block may be on a 576px row. The real width lands on the nearest
# whole number of pixels per module at or below this (see render_qr).
QR_TARGET_PX = 420
# Desk popup QR — large enough to scan from across the counter.
SCREEN_QR_PX = 840
# ISO/IEC 18004 requires a 4-module quiet zone. The slip used to ship 2.
QR_QUIET_MODULES = 4


def guest_scheme() -> str:
    if os.environ.get("BYOI_GUEST_TLS", "1") == "0":
        return "http"
    return "https"


def lan_ip() -> str:
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))
        ip = sock.getsockname()[0]
        sock.close()
        return ip
    except OSError:
        return "127.0.0.1"


def public_base(url: str, default_port: int = 8787, *, public_host: str | None = None) -> str:
    """The address a guest's phone actually opens.

    In the cloud that is the session's own hostname on the edge, with a real
    certificate and no port — so it is short enough to read off a slip and no
    browser warns about it. On a salon PC it is still ``https://<lan-ip>:8787``,
    where the seat holds the salon CA's certificate itself.
    """
    override = os.environ.get("BYOI_JOIN_BASE", "").rstrip("/")
    if override:
        return override
    if public_host:
        return f"https://{public_host}"
    raw = url if "://" in url else f"http://{url}"
    parsed = urlparse(raw)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or default_port
    if host in LOOPBACK_HOSTS:
        host = lan_ip()
    return f"{guest_scheme()}://{host}:{port}"


def public_host(url: str, default_port: int = 8787) -> str:
    parsed = urlparse(public_base(url, default_port))
    return parsed.hostname or lan_ip()


def join_base() -> str:
    """Where the desk itself is reachable."""
    override = os.environ.get("BYOI_JOIN_BASE", "").rstrip("/")
    if override:
        return override
    public = os.environ.get("BYOI_PUBLIC_BASE", "").rstrip("/")
    if public:
        return public
    port = os.environ.get("BYOI_HOUSE_PORT", "8080")
    return f"http://{lan_ip()}:{port}"


def join_url(otp: str) -> str:
    return f"{join_base()}/guest/?otp={otp}"


def seat_join_url(seat: dict, otp: str) -> str:
    base = public_base(
        seat.get("agent_url") or "http://127.0.0.1:8787",
        public_host=seat.get("public_host"),
    )
    return f"{base}/join?otp={otp}"


def render_qr(data: str, *, target_px: int = QR_TARGET_PX) -> tuple[Image.Image, dict]:
    """Render a QR at a whole number of pixels per module, never rescaled.

    Slips are thresholded rather than dithered, so a fractional rescale is not
    survivable: at 33 modules scaled into 360px each module wanted 10.9px, the
    threshold snapped edges to whichever side of the pixel they fell, and
    neighbouring modules printed 10px and 11px wide. A phone reads that jitter
    as grid drift, which good light hides and a cafe's does not. So the pitch is
    chosen to divide evenly and the raster is handed to the print head as-is.

    Returns the image plus the geometry, which the scan report asserts against.
    """
    qr = qrcode.QRCode(
        border=QR_QUIET_MODULES,
        box_size=1,
        error_correction=qrcode.constants.ERROR_CORRECT_Q,
    )
    qr.add_data(data)
    qr.make(fit=True)
    grid = len(qr.get_matrix())  # data modules plus quiet zone on both sides
    box = max(1, target_px // grid)
    qr.box_size = box
    image = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    geometry = {
        "version": qr.version,
        "grid": grid,
        "modules": grid - 2 * QR_QUIET_MODULES,
        "px_per_module": box,
        "quiet_modules": QR_QUIET_MODULES,
        "size_px": image.size[0],
        "module_mm": round(box / A6_304.dpi * 25.4, 3),
        "error_correction": "Q",
    }
    if image.size != (box * grid, box * grid):
        raise ValueError(f"QR raster {image.size} is not {box}px/module on a {grid} grid")
    return image, geometry


def save_join_qr(join: str, dest_dir: Path, *, target_px: int = SCREEN_QR_PX) -> Path:
    """Write a QR-only PNG for the desk popup (no slip text)."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    image, _ = render_qr(join, target_px=target_px)
    path = dest_dir / "last-qr.png"
    image.save(path, format="PNG")
    return path


def compose_checkin_slip(
    *,
    salon: str,
    seat_name: str,
    coder_name: str,
    board_title: str | None,
    otp: str,
    wellness_minutes: int,
    break_after: int,
    wifi_ssid: str | None,
    join: str,
) -> Image.Image:
    width = A6_304.row_width
    qr_img, _ = render_qr(join)

    header = render_text(
        f"{salon}\n{seat_name}\n{coder_name}",
        font_size=32,
        margin=12,
    )
    parsed = urlparse(join)
    hostport = parsed.netloc or join
    body_lines = [board_title or "(pick a brief at the seat)"]
    if wifi_ssid:
        # Only true when the seat is a PC in this room. A cloud seat is reached
        # over whatever network the phone already has, and telling a guest to
        # join the cafe Wi-Fi would send them looking for a password they do
        # not need.
        body_lines.append(f"Same Wi-Fi as this PC  {wifi_ssid}")
    body_lines += [
        "Open BYOI Guest · scan this QR",
        f"OTP  {otp}",
        hostport,
        "Phone chat · Claude Code on this seat",
        f"Session {wellness_minutes} min · break after {break_after} min",
    ]
    body = render_text("\n".join(body_lines), font_size=22, margin=12)

    gap = 16
    height = header.size[1] + qr_img.size[1] + body.size[1] + gap * 4 + 40
    canvas = Image.new("RGB", (width, height), (255, 255, 255))
    y = gap
    canvas.paste(header, (0, y))
    y += header.size[1] + gap
    qx = (width - qr_img.size[0]) // 2
    canvas.paste(qr_img, (qx, y))
    y += qr_img.size[1] + gap
    canvas.paste(body, (0, y))
    draw = ImageDraw.Draw(canvas)
    draw.rectangle((8, 8, width - 9, height - 9), outline=(0, 0, 0), width=2)
    return canvas


def png_bytes(image: Image.Image) -> bytes:
    buf = BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()
