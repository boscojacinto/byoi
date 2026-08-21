"""Run the mTLS control app on :8788 inside the same process as the guest HTTP app."""

from __future__ import annotations

import asyncio
import os
import ssl

import uvicorn

from apps.tls import paths, seat_ssl_context

from . import control


class _QuietServer(uvicorn.Server):
    def install_signal_handlers(self) -> None:
        return


def control_port() -> int:
    return int(os.environ.get("BYOI_CONTROL_PORT", "8788"))


def control_bind() -> str:
    return os.environ.get("BYOI_CONTROL_BIND", "0.0.0.0")


def want_control_server() -> bool:
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return False
    if os.environ.get("BYOI_TLS", "1") == "0":
        return False
    return paths().seat_ready()


def control_config(*, host: str | None = None, port: int | None = None) -> uvicorn.Config:
    return uvicorn.Config(
        app=control.app,
        host=host or control_bind(),
        port=port if port is not None else control_port(),
        ssl_certfile=str(paths().seat_cert),
        ssl_keyfile=str(paths().seat_key),
        ssl_ca_certs=str(paths().ca),
        ssl_cert_reqs=ssl.CERT_REQUIRED,
        log_level="warning",
        lifespan="off",
        access_log=False,
    )


async def serve_control(server: _QuietServer) -> None:
    await server.serve()


def spawn_control_task() -> tuple[_QuietServer, asyncio.Task] | None:
    if not want_control_server():
        return None
    # Touch the context so missing certs fail at startup, not on first request.
    seat_ssl_context()
    server = _QuietServer(control_config())
    task = asyncio.create_task(serve_control(server))
    return server, task
