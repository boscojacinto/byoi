"""Salon CA + mTLS files. Identity is the certificate, not a host IP."""

from __future__ import annotations

import os
import socket
import ssl
import subprocess
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SEAT_SERVER_NAME = os.environ.get("BYOI_SEAT_TLS_NAME", "byoi-seat-1")
HOST_CLIENT_NAME = os.environ.get("BYOI_HOST_TLS_NAME", "byoi-host")


def tls_dir() -> Path:
    return Path(os.environ.get("BYOI_TLS_DIR", ROOT / "data" / "tls"))


@dataclass(frozen=True)
class TlsPaths:
    root: Path

    @property
    def ca(self) -> Path:
        return self.root / "ca.pem"

    @property
    def ca_key(self) -> Path:
        return self.root / "ca-key.pem"

    @property
    def seat_cert(self) -> Path:
        return self.root / "seat.pem"

    @property
    def seat_key(self) -> Path:
        return self.root / "seat-key.pem"

    @property
    def host_cert(self) -> Path:
        return self.root / "host.pem"

    @property
    def host_key(self) -> Path:
        return self.root / "host-key.pem"

    @property
    def token(self) -> Path:
        return self.root / "host.token"

    def seat_ready(self) -> bool:
        return self.ca.is_file() and self.seat_cert.is_file() and self.seat_key.is_file()

    def host_ready(self) -> bool:
        return self.ca.is_file() and self.host_cert.is_file() and self.host_key.is_file()


def paths() -> TlsPaths:
    return TlsPaths(tls_dir())


def local_ipv4s() -> list[str]:
    ips = {"127.0.0.1"}
    extra = os.environ.get("BYOI_TLS_SANS", "")
    for part in extra.split(","):
        part = part.strip()
        if part:
            ips.add(part)
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))
        ips.add(sock.getsockname()[0])
        sock.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127."):
                ips.add(ip)
    except OSError:
        pass
    return sorted(ips)


def guest_tls_is_ours() -> bool:
    """True when the seat terminates guest HTTPS itself.

    On a salon PC it does, so the certificate has to carry the seat's current
    LAN IPs — that is the address the phone opens. In the cloud Caddy holds a
    real certificate for a real name and the seat only speaks TLS on its control
    port, where the address is irrelevant.
    """
    return os.environ.get("BYOI_GUEST_TLS", "1") != "0"


def seat_san_line(
    name: str = SEAT_SERVER_NAME,
    *,
    extra_dns: tuple[str, ...] = (),
    with_ips: bool | None = None,
) -> str:
    names = [f"DNS:{name}", "DNS:localhost", *(f"DNS:{d}" for d in extra_dns)]
    if guest_tls_is_ours() if with_ips is None else with_ips:
        names.extend(f"IP:{ip}" for ip in local_ipv4s())
    return "subjectAltName=" + ",".join(names)


def issue_seat_cert(
    ca: TlsPaths,
    out: TlsPaths | None = None,
    *,
    name: str = SEAT_SERVER_NAME,
    extra_dns: tuple[str, ...] = (),
    with_ips: bool | None = None,
) -> TlsPaths:
    """Sign a seat server certificate with ``ca``, writing it into ``out``.

    ``out`` defaults to the CA's own directory, which is the single-seat salon
    PC case. The cloud passes a per-session directory instead, so each seat
    container gets its own key and the desk can hand it exactly one identity.
    """
    out = out or ca
    dest = out.root
    dest.mkdir(parents=True, exist_ok=True)
    if not out.seat_key.is_file():
        _openssl("genrsa", "-out", str(out.seat_key), "2048")
        os.chmod(out.seat_key, 0o600)
    ext = dest / "seat.ext"
    ext.write_text(
        "basicConstraints=CA:FALSE\n"
        "keyUsage=digitalSignature,keyEncipherment\n"
        "extendedKeyUsage=serverAuth\n"
        + seat_san_line(name, extra_dns=extra_dns, with_ips=with_ips)
        + "\n"
    )
    _openssl("req", "-new", "-key", str(out.seat_key), "-out", str(dest / "seat.csr"),
             "-subj", f"/CN={name}")
    _openssl("x509", "-req", "-in", str(dest / "seat.csr"), "-CA", str(ca.ca),
             "-CAkey", str(ca.ca_key), "-CAcreateserial", "-out", str(out.seat_cert),
             "-days", "825", "-sha256", "-extfile", str(ext))
    if out.root != ca.root and not out.ca.is_file():
        out.ca.write_bytes(ca.ca.read_bytes())
    (dest / "seat.csr").unlink(missing_ok=True)
    ext.unlink(missing_ok=True)
    (dest / "ca.srl").unlink(missing_ok=True)
    (ca.root / "ca.srl").unlink(missing_ok=True)
    return out


def copy_ca_to_guest(p: TlsPaths | None = None) -> Path | None:
    p = p or paths()
    if not p.ca.is_file():
        return None
    dest = ROOT / "apps" / "guest" / "assets" / "ca.pem"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(p.ca.read_bytes())
    return dest


def generate(dest: Path | None = None) -> TlsPaths:
    """Create (or refresh) the salon CA, seat server cert, host client cert, and token."""
    dest = dest or tls_dir()
    dest.mkdir(parents=True, exist_ok=True)
    os.chmod(dest, 0o700)
    p = TlsPaths(dest)
    if not p.ca.is_file():
        _openssl("req", "-x509", "-newkey", "rsa:2048", "-sha256", "-days", "3650", "-nodes",
                 "-keyout", str(p.ca_key), "-out", str(p.ca),
                 "-subj", "/CN=BYOI salon CA")
        os.chmod(p.ca_key, 0o600)
    issue_seat_cert(p)
    if not p.host_cert.is_file() or not p.host_key.is_file():
        host_ext = dest / "host.ext"
        host_ext.write_text(
            "basicConstraints=CA:FALSE\n"
            "keyUsage=digitalSignature\n"
            "extendedKeyUsage=clientAuth\n"
            f"subjectAltName=DNS:{HOST_CLIENT_NAME}\n"
        )
        _openssl("req", "-newkey", "rsa:2048", "-nodes",
                 "-keyout", str(p.host_key), "-out", str(dest / "host.csr"),
                 "-subj", f"/CN={HOST_CLIENT_NAME}")
        _openssl("x509", "-req", "-in", str(dest / "host.csr"), "-CA", str(p.ca),
                 "-CAkey", str(p.ca_key), "-CAcreateserial", "-out", str(p.host_cert),
                 "-days", "825", "-sha256", "-extfile", str(host_ext))
        os.chmod(p.host_key, 0o600)
        (dest / "host.csr").unlink(missing_ok=True)
        host_ext.unlink(missing_ok=True)
        (dest / "ca.srl").unlink(missing_ok=True)
    if not p.token.is_file():
        token = subprocess.check_output(["openssl", "rand", "-hex", "32"], text=True).strip()
        p.token.write_text(token + "\n", encoding="utf-8")
        os.chmod(p.token, 0o600)
    if not os.environ.get("PYTEST_CURRENT_TEST"):
        copy_ca_to_guest(p)
    return p


def _openssl(*args: str) -> None:
    subprocess.check_call(["openssl", *args], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def seat_ssl_context() -> ssl.SSLContext:
    p = paths()
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.load_cert_chain(str(p.seat_cert), str(p.seat_key))
    ctx.load_verify_locations(str(p.ca))
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


def guest_verify_context() -> ssl.SSLContext:
    """Trust the salon CA as a client of the seat's guest HTTPS (no client cert)."""
    p = paths()
    ctx = ssl.create_default_context(purpose=ssl.Purpose.SERVER_AUTH, cafile=str(p.ca))
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


def host_ssl_context() -> ssl.SSLContext:
    """Trust the salon CA and present the host client certificate.

    Hostname checks are off so the desk can use the seat's current LAN IP.
    Identity is the CA-signed cert, not the IP.
    """
    p = paths()
    ctx = ssl.create_default_context(purpose=ssl.Purpose.SERVER_AUTH, cafile=str(p.ca))
    ctx.load_cert_chain(str(p.host_cert), str(p.host_key))
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx
