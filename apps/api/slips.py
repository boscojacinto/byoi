"""Compose 576px thermal slips for the PeriPage A6."""

from __future__ import annotations

import os
import socket
from io import BytesIO
from urllib.parse import urlparse

import qrcode
from PIL import Image, ImageDraw

from peripage_a6.models import A6_304
from peripage_a6.raster import render_text

WIFI_SSID = os.environ.get("BYOI_WIFI_SSID", "salon Wi-Fi")
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def lan_ip() -> str:
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))
        ip = sock.getsockname()[0]
        sock.close()
        return ip
    except OSError:
        return "127.0.0.1"


def public_base(url: str, default_port: int = 8787) -> str:
    """Turn a seat agent URL into something a phone on cafe Wi-Fi can open."""
    override = os.environ.get("BYOI_JOIN_BASE", "").rstrip("/")
    if override:
        return override
    raw = url if "://" in url else f"http://{url}"
    parsed = urlparse(raw)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or default_port
    if host in LOOPBACK_HOSTS:
        host = lan_ip()
    scheme = parsed.scheme or "http"
    return f"{scheme}://{host}:{port}"


def public_host(url: str, default_port: int = 8787) -> str:
    parsed = urlparse(public_base(url, default_port))
    return parsed.hostname or lan_ip()


def join_base() -> str:
    """House PWA on cafe Wi-Fi (same laptop or desk host)."""
    override = os.environ.get("BYOI_JOIN_BASE", "").rstrip("/")
    if override:
        return override
    port = os.environ.get("BYOI_HOUSE_PORT", "8080")
    return f"http://{lan_ip()}:{port}"


def join_url(otp: str) -> str:
    return f"{join_base()}/coder?otp={otp}"


def seat_join_url(seat: dict, otp: str) -> str:
    base = public_base(seat.get("agent_url") or "http://127.0.0.1:8787")
    return f"{base}/join?otp={otp}"


def compose_checkin_slip(
    *,
    salon: str,
    seat_name: str,
    coder_name: str,
    board_title: str | None,
    otp: str,
    wellness_minutes: int,
    break_after: int,
    wifi_ssid: str,
    join: str,
) -> Image.Image:
    width = A6_304.row_width
    qr = qrcode.QRCode(border=2, box_size=6, error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(join)
    qr.make(fit=True)
    qr_img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    qr_img = qr_img.resize((360, 360))

    header = render_text(
        f"{salon}\n{seat_name}\n{coder_name}",
        font_size=32,
        margin=12,
    )
    parsed = urlparse(join)
    hostport = parsed.netloc or join
    body_lines = [
        board_title or "(pick a brief at the seat)",
        f"Same Wi-Fi as this PC  {wifi_ssid}",
        "Scan QR on your phone.",
        hostport,
        "Browser TTY → tmux claude-guest",
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
