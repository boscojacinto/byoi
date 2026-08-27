#!/usr/bin/env python3
"""Print the salon's slips at the venue, from a desk that is in the cloud.

The PeriPage A6 speaks Bluetooth LE, which is a property of the room. So the
desk composes the slip and queues it; this runs on a machine at the counter,
claims jobs, and puts them through the same driver the salon PC always used.

    export BYOI_DESK_URL=https://salon.example.com
    export BYOI_PRINT_RELAY_TOKEN=...          # or data/secrets/print-relay.token
    export PERIPAGE_MAC=C6:6C:09:0B:B2:50      # unset -> dump the protocol instead
    ./scripts/print-relay.py

Nothing here needs to be reachable from the internet: it only makes outbound
requests, so the counter can sit behind whatever NAT the cafe has.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

import httpx  # noqa: E402

from apps.api.printing import print_slip  # noqa: E402
from apps.secrets import read_secret  # noqa: E402
from PIL import Image  # noqa: E402

POLL_S = 2.0
BACKOFF_MAX_S = 30.0


def desk_url() -> str:
    url = os.environ.get("BYOI_DESK_URL", "").strip().rstrip("/")
    if not url:
        raise SystemExit("set BYOI_DESK_URL to the salon desk, e.g. https://salon.example.com")
    return url


def relay_token() -> str:
    token = read_secret("BYOI_PRINT_RELAY_TOKEN")
    if not token:
        raise SystemExit(
            "no relay token — run ./scripts/salon-secrets.sh print-relay on the desk, "
            "then put the same value here as BYOI_PRINT_RELAY_TOKEN"
        )
    return token


def claim_one(client: httpx.Client) -> dict | None:
    res = client.get("/api/print/next")
    if res.status_code == 204:
        return None
    res.raise_for_status()
    return res.json()


def print_one(client: httpx.Client, job: dict, spool: Path) -> None:
    png = spool / f"{job['id']}.png"
    image_res = client.get(job["png"])
    image_res.raise_for_status()
    png.write_bytes(image_res.content)

    try:
        result = print_slip(Image.open(png), spool)
    except Exception as exc:  # noqa: BLE001 - the desk needs to hear about any of these
        # An empty roll, a flat battery, or a dropped LE link. Report it rather
        # than dying: the operator sees the printer go red on the floor screen.
        client.post(f"/api/print/{job['id']}/done", json={"ok": False, "error": str(exc)[:400]})
        print(f"job {job['id']}: {exc}", file=sys.stderr)
        return

    client.post(f"/api/print/{job['id']}/done", json={"ok": True})
    print(f"job {job['id']}: printed ({result.get('mode')})")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="claim at most one job and exit")
    parser.add_argument("--spool", default=str(ROOT / "data" / "print-relay"))
    args = parser.parse_args()

    spool = Path(args.spool).expanduser()
    spool.mkdir(parents=True, exist_ok=True)

    url = desk_url()
    client = httpx.Client(
        base_url=url,
        headers={"Authorization": f"Bearer {relay_token()}"},
        timeout=30.0,
    )
    mac = os.environ.get("PERIPAGE_MAC", "").strip()
    print(f"relay: {url} -> {mac or 'no PERIPAGE_MAC (dumping the protocol instead)'}")

    backoff = POLL_S
    with client:
        while True:
            try:
                job = claim_one(client)
                backoff = POLL_S
            except httpx.HTTPError as exc:
                # The cafe's uplink, or the desk restarting. Keep the printer
                # attached and keep trying; queued slips are still queued.
                print(f"desk unreachable: {exc}", file=sys.stderr)
                time.sleep(backoff)
                backoff = min(backoff * 2, BACKOFF_MAX_S)
                continue

            if job:
                print_one(client, job, spool)
                if args.once:
                    return 0
                continue

            if args.once:
                print("nothing queued")
                return 0
            time.sleep(POLL_S)


if __name__ == "__main__":
    raise SystemExit(main())
