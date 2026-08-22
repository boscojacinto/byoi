#!/usr/bin/env python3
"""Start an isolated salon + fake Claude, then drive desk and guest in Chrome."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import websockets

ROOT = Path(__file__).resolve().parents[1]
SIM = ROOT / "data" / "sim-failover"
SHOTS = SIM / "screenshots"
DOWNLOADS = SIM / "downloads"
ACCOUNTS = SIM / "claude-accounts"
HANDOFFS = SIM / "handoffs"
DATA = SIM / "salon"
FAKE = ROOT / "scripts" / "fake-claude.py"
TLS = ROOT / "data" / "tls"

DESK = 18080
SEAT = 18787
CONTROL = 18788
CHROME_DEBUG = 19333
CHILDREN: list[subprocess.Popen] = []


class Cdp:
    def __init__(self, ws_url: str) -> None:
        self.ws_url = ws_url
        self._id = 0
        self._ws: Any = None

    async def connect(self) -> None:
        self._ws = await websockets.connect(self.ws_url, max_size=20_000_000)

    async def close(self) -> None:
        if self._ws is not None:
            await self._ws.close()

    async def call(self, method: str, **params):
        assert self._ws is not None
        self._id += 1
        msg_id = self._id
        await self._ws.send(json.dumps({"id": msg_id, "method": method, "params": params}))
        while True:
            raw = await asyncio.wait_for(self._ws.recv(), timeout=30)
            data = json.loads(raw)
            if data.get("id") != msg_id:
                continue
            if "error" in data:
                raise RuntimeError(f"{method}: {data['error']}")
            return data.get("result") or {}

    async def eval(self, expression: str, timeout: float = 15.0):
        result = await self.call(
            "Runtime.evaluate",
            expression=expression,
            awaitPromise=True,
            returnByValue=True,
        )
        if result.get("exceptionDetails"):
            text = result["exceptionDetails"].get("text") or result["exceptionDetails"]
            raise RuntimeError(f"js: {text} :: {expression[:180]}")
        return (result.get("result") or {}).get("value")

    async def wait(self, expression: str, timeout: float = 15.0):
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            try:
                value = await self.eval(expression)
            except Exception as exc:  # noqa: BLE001
                last = repr(exc)
                await asyncio.sleep(0.2)
                continue
            if value is True or value not in (None, False, "", 0):
                return value
            last = value
            await asyncio.sleep(0.2)
        raise TimeoutError(f"{expression} last={last!r}")

    async def goto(self, url: str) -> None:
        await self.call("Page.enable")
        await self.call("Runtime.enable")
        await self.call("Page.navigate", url=url)
        await self.wait("document.readyState === 'complete'", 20)

    async def screenshot(self, path: Path) -> None:
        data = await self.call("Page.captureScreenshot", format="png", fromSurface=True)
        path.write_bytes(base64.b64decode(data["data"]))

    async def set_viewport(self, width: int, height: int, mobile: bool = False) -> None:
        await self.call(
            "Emulation.setDeviceMetricsOverride",
            width=width,
            height=height,
            deviceScaleFactor=2 if mobile else 1,
            mobile=mobile,
        )


def wait_http(url: str, timeout: float = 20.0) -> None:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=2)
            return
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(0.2)
    raise SystemExit(f"timeout waiting for {url}: {last}")


def spawn(name: str, argv: list[str], env: dict[str, str], log_name: str) -> subprocess.Popen:
    log = (SIM / log_name).open("w", encoding="utf-8")
    proc = subprocess.Popen(
        argv,
        cwd=str(ROOT),
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    CHILDREN.append(proc)
    return proc


def stop_all() -> None:
    for proc in CHILDREN:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    deadline = time.time() + 5
    for proc in CHILDREN:
        while proc.poll() is None and time.time() < deadline:
            time.sleep(0.1)
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass


def base_env() -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = f"{ROOT}/src:{ROOT}" + (f":{env['PYTHONPATH']}" if env.get("PYTHONPATH") else "")
    env["BYOI_DATA"] = str(DATA)
    env["BYOI_TLS_DIR"] = str(TLS)
    env["BYOI_HOST_TOKEN_FILE"] = str(TLS / "host.token")
    env["BYOI_CLAUDE_ACCOUNTS_DIR"] = str(ACCOUNTS)
    env["BYOI_HANDOFFS_DIR"] = str(HANDOFFS)
    env["BYOI_WORKSPACE"] = str(SIM / "workspace")
    env["BYOI_CLAUDE"] = str(FAKE)
    env["BYOI_QUOTA_FAILOVER_PCT"] = "80"
    env["BYOI_HOUSE_URL"] = f"http://127.0.0.1:{DESK}"
    env["BYOI_SEAT_CONTROL_URL"] = f"https://127.0.0.1:{CONTROL}"
    env["BYOI_SEAT_URL"] = f"http://127.0.0.1:{SEAT}"
    env["BYOI_CONTROL_PORT"] = str(CONTROL)
    env["BYOI_GUEST_TLS"] = "0"
    env["BYOI_TLS"] = "1"
    env["BYOI_SEAT_ID"] = "seat-1"
    return env


def prepare() -> None:
    if SIM.exists():
        shutil.rmtree(SIM)
    for folder in (SHOTS, DOWNLOADS, HANDOFFS, DATA, SIM / "workspace"):
        folder.mkdir(parents=True, exist_ok=True)
    for label in ("claude-seat-1", "claude-seat-2"):
        dest = ACCOUNTS / label
        dest.mkdir(parents=True, exist_ok=True)
        (dest / ".credentials.json").write_text("{}\n", encoding="utf-8")
    FAKE.chmod(0o755)
    (SIM / "workspace" / "README.md").write_text("sim workspace\n", encoding="utf-8")


def patch_seat_url() -> None:
    db = DATA / "salon.db"
    deadline = time.time() + 10
    while not db.is_file() and time.time() < deadline:
        time.sleep(0.1)
    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE seats SET agent_url=? WHERE id=?",
        (f"http://127.0.0.1:{SEAT}", "seat-1"),
    )
    conn.commit()
    conn.close()


def chrome_pages() -> list[dict]:
    with urllib.request.urlopen(f"http://127.0.0.1:{CHROME_DEBUG}/json/list", timeout=5) as res:
        return json.loads(res.read())


def page_ws(url: str) -> str:
    prefix = url.rstrip("/")
    for page in chrome_pages():
        if page.get("type") not in {None, "page"}:
            continue
        href = page.get("url") or ""
        if href.startswith(prefix) and page.get("webSocketDebuggerUrl"):
            return page["webSocketDebuggerUrl"]
    raise RuntimeError(f"no chrome tab for {url}: {chrome_pages()}")


async def attach(url: str) -> Cdp:
    cdp = Cdp(page_ws(url))
    await cdp.connect()
    await cdp.call("Page.enable")
    await cdp.call("Runtime.enable")
    return cdp


async def open_tab(url: str) -> Cdp:
    with urllib.request.urlopen(f"http://127.0.0.1:{CHROME_DEBUG}/json/version", timeout=5) as res:
        browser_ws = json.loads(res.read())["webSocketDebuggerUrl"]
    browser = Cdp(browser_ws)
    await browser.connect()
    created = await browser.call("Target.createTarget", url=url)
    target_id = created.get("targetId")
    await browser.close()
    prefix = url.split("?")[0].rstrip("/")
    deadline = time.time() + 12
    ws = None
    while time.time() < deadline:
        for page in chrome_pages():
            if page.get("type") not in {None, "page"}:
                continue
            href = page.get("url") or ""
            pid = page.get("id")
            if (pid == target_id or href.startswith(prefix)) and page.get("webSocketDebuggerUrl"):
                ws = page["webSocketDebuggerUrl"]
                break
        if ws:
            break
        await asyncio.sleep(0.2)
    if not ws:
        raise RuntimeError(f"chrome did not open {url}: {chrome_pages()}")
    cdp = Cdp(ws)
    await cdp.connect()
    await cdp.call("Page.enable")
    await cdp.call("Runtime.enable")
    return cdp


async def run_browser_async() -> None:
    desk_url = f"http://127.0.0.1:{DESK}/"
    guest_base = f"http://127.0.0.1:{SEAT}/guest/"
    failures: list[str] = []
    chrome_env = os.environ.copy()
    chrome_env["DISPLAY"] = os.environ.get("DISPLAY", ":0")
    profile = SIM / "chrome-profile"
    profile.mkdir(parents=True, exist_ok=True)
    spawn(
        "chrome",
        [
            "google-chrome",
            f"--remote-debugging-port={CHROME_DEBUG}",
            "--remote-allow-origins=*",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-extensions",
            "--disable-sync",
            "--window-size=1280,800",
            desk_url,
        ],
        chrome_env,
        "chrome.log",
    )
    wait_http(f"http://127.0.0.1:{CHROME_DEBUG}/json/version", timeout=25)
    time.sleep(0.6)

    desk = await attach(desk_url)
    guest = None
    await desk.set_viewport(1280, 800)
    try:
        await desk.wait("document.getElementById('openSit') !== null", 20)
        await desk.eval("document.getElementById('openSit').click(); true")
        await desk.wait("document.getElementById('sitModal') && !document.getElementById('sitModal').hidden", 8)
        await desk.eval(
            """
            document.getElementById('coderName').value = 'Sim Guest';
            document.getElementById('coderName').dispatchEvent(new Event('input', {bubbles:true}));
            document.getElementById('checkin').requestSubmit();
            true
            """
        )
        await desk.wait("Boolean(document.getElementById('qrStage') && !document.getElementById('qrStage').hidden)", 15)
        await desk.screenshot(SHOTS / "01-desk-checkin-qr.png")

        seats = json.loads(urllib.request.urlopen(f"{desk_url}api/seats").read())
        live = next(s for s in seats["seats"] if s.get("session"))
        otp = live["session"]["unlock_otp"]
        if not otp:
            raise AssertionError("check-in did not store an OTP")

        guest = await open_tab(f"{guest_base}?otp={otp}")
        await guest.set_viewport(390, 844, mobile=True)
        await guest.wait(
            "Boolean(document.getElementById('open-chat') || document.getElementById('sit'))",
            20,
        )
        await guest.eval(
            """(() => {
              if (document.getElementById('open-chat')) return true;
              const sit = document.getElementById('sit');
              const otp = document.getElementById('otp');
              if (sit && otp) {
                sit.click();
              }
              return true;
            })()"""
        )
        await guest.wait("Boolean(document.getElementById('open-chat') && !document.getElementById('open-chat').disabled)", 20)
        await guest.screenshot(SHOTS / "02-guest-floor.png")
        await guest.eval(
            """
            const btn = document.querySelector('[data-claim]');
            if (btn) btn.click();
            true
            """
        )
        await asyncio.sleep(0.4)
        await guest.eval("document.getElementById('open-chat').click(); true")
        await guest.wait("document.getElementById('draft') !== null", 15)
        await guest.screenshot(SHOTS / "03-guest-chat-ready.png")

        await guest.wait("document.getElementById('draft') !== null && document.getElementById('composer') !== null", 10)
        await guest.eval(
            """(() => {
              const box = document.getElementById('draft');
              const form = document.getElementById('composer');
              if (!box || !form) return false;
              box.value = 'fix the slip';
              box.dispatchEvent(new Event('input', {bubbles:true}));
              const send = document.getElementById('send');
              if (send) send.click();
              else form.requestSubmit();
              return true;
            })()"""
        )
        await guest.wait("document.getElementById('log').innerText.includes('QR contrast')", 20)
        await guest.screenshot(SHOTS / "04-guest-first-reply.png")
        await guest.wait("document.getElementById('log').innerText.toLowerCase().includes('continuing on')", 25)
        await guest.wait("document.getElementById('log').innerText.includes('spare account')", 15)
        await guest.screenshot(SHOTS / "05-guest-after-switch.png")

        log = await guest.eval("document.getElementById('log').innerText")
        label = await guest.eval("document.getElementById('chat-label').innerText")
        if "I'll bump the QR contrast" not in (log or ""):
            failures.append("guest lost the first assistant reply after switch")
        if "hit a usage limit" not in (log or "") and "spare" not in (log or "").lower():
            failures.append("guest did not show the account-switch system line")
        if "claude-seat-2" not in (label or "") and "claude-seat-2" not in (log or ""):
            failures.append(f"guest chrome missing spare account (label={label!r})")

        await guest.wait("document.getElementById('draft') !== null && document.getElementById('composer') !== null", 10)
        await guest.eval(
            """(() => {
              const box = document.getElementById('draft');
              const form = document.getElementById('composer');
              if (!box || !form) return false;
              box.value = 'still there?';
              box.dispatchEvent(new Event('input', {bubbles:true}));
              const send = document.getElementById('send');
              if (send) send.click();
              else form.requestSubmit();
              return true;
            })()"""
        )
        await guest.wait("document.getElementById('log').innerText.includes('Still here on the spare login')", 15)
        await guest.screenshot(SHOTS / "06-guest-second-turn.png")

        await guest.eval("document.getElementById('slash').click(); true")
        await guest.wait("document.querySelector('[data-slash=\"/handoff\"]') !== null", 8)
        await guest.eval("document.querySelector('[data-slash=\"/handoff\"]').click(); true")
        await asyncio.sleep(0.8)
        ticket = await guest.eval(
            """
            (() => {
              try { return JSON.parse(sessionStorage.getItem('byoi.guest') || '{}').ticket || ''; }
              catch { return ''; }
            })()
            """
        )
        if not ticket:
            failures.append("guest ticket missing after chat unlock")
        else:
            req = urllib.request.Request(f"http://127.0.0.1:{SEAT}/local/handoff?ticket={ticket}")
            try:
                with urllib.request.urlopen(req, timeout=8) as res:
                    body = res.read().decode("utf-8")
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")
                failures.append(f"handoff HTTP {exc.code}: {body[:200]}")
                body = ""
            (DOWNLOADS / "byoi-handoff.md").write_text(body, encoding="utf-8")
            if body and "QR contrast" not in body and "scan test" not in body:
                failures.append(f"handoff download missing compact summary: {body[:200]!r}")
        await guest.screenshot(SHOTS / "07-guest-handoff.png")

        await desk.goto(desk_url)
        await asyncio.sleep(0.8)
        floor = await desk.eval("document.getElementById('seats').innerText")
        today = await desk.eval("document.getElementById('today').innerText")
        html = await desk.eval("document.getElementById('seats').innerHTML")
        await desk.screenshot(SHOTS / "08-desk-floor-after.png")
        if "Sim Guest" not in (floor or ""):
            failures.append("desk floor lost the sitting guest")
        if "claude-seat-2" not in (floor or "") and "Handoff" not in (html or ""):
            failures.append(f"desk floor missing spare account or handoff (floor={floor!r})")
        if "Claude login" not in (today or "") and "ready" not in (today or "").lower():
            failures.append(f"desk hero missing login capacity (today={today!r})")

        await desk.eval("document.querySelector('[data-pane=live]').click(); true")
        await desk.wait("document.getElementById('live') !== null", 8)
        await asyncio.sleep(0.6)
        live_text = await desk.eval("document.getElementById('live').innerText")
        await desk.screenshot(SHOTS / "09-desk-live.png")
        if "fix the slip" not in (live_text or ""):
            failures.append(f"desk live missing guest transcript: {(live_text or '')[:240]!r}")

        await desk.set_viewport(390, 844, mobile=True)
        await desk.goto(desk_url)
        await asyncio.sleep(0.4)
        await desk.screenshot(SHOTS / "10-desk-mobile.png")
    except Exception as exc:
        try:
            await desk.screenshot(SHOTS / "fail-desk.png")
        except Exception:
            pass
        if guest is not None:
            try:
                await guest.screenshot(SHOTS / "fail-guest.png")
            except Exception:
                pass
        failures.append(f"{type(exc).__name__}: {exc}")
    finally:
        await desk.close()
        if guest is not None:
            try:
                await guest.close()
            except Exception:
                pass

    if failures:
        print("FAIL")
        for item in failures:
            print(f" - {item}")
        raise SystemExit(1)
    print("OK")
    print(f"screenshots: {SHOTS}")
    print(f"handoff: {DOWNLOADS}")


def run_browser() -> None:
    asyncio.run(run_browser_async())


def main() -> None:
    if not (TLS / "host.token").is_file() or not (TLS / "seat.pem").is_file():
        raise SystemExit("run ./scripts/salon-tls.sh first")
    prepare()
    env = base_env()
    py = str(ROOT / ".venv" / "bin" / "python")
    if not Path(py).exists():
        py = sys.executable
    try:
        spawn(
            "desk",
            [py, "-m", "uvicorn", "apps.api.main:app", "--host", "127.0.0.1", "--port", str(DESK)],
            env,
            "desk.log",
        )
        wait_http(f"http://127.0.0.1:{DESK}/api/health")
        patch_seat_url()
        spawn(
            "seat",
            [py, "-m", "uvicorn", "apps.seat.main:app", "--host", "127.0.0.1", "--port", str(SEAT)],
            env,
            "seat.log",
        )
        wait_http(f"http://127.0.0.1:{SEAT}/local/status")
        run_browser()
    finally:
        stop_all()


if __name__ == "__main__":
    main()
