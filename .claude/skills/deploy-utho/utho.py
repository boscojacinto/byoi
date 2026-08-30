#!/usr/bin/env python3
"""The Utho cloud API, reduced to what the salon wants from a provider.

Which plans exist, an SSH key, one VM, its IP, and a way to take it down again.
Nothing here knows what a seat is: this creates the box that ``cloud-up.sh``
then runs on.

    ./utho.py check                      # token works, and what it is spending
    ./utho.py zones                      # dcslug values
    ./utho.py plans --min-ram 16         # planid values
    ./utho.py images [ubuntu]            # image values
    ./utho.py keys
    ./utho.py key-import salon ~/.ssh/id_ed25519.pub
    ./utho.py create salon.example.com
    ./utho.py list
    ./utho.py wait salon.example.com     # block until Running and sshd answers
    ./utho.py ip salon.example.com
    ./utho.py show salon.example.com
    ./utho.py destroy salon.example.com

The token comes from ``$UTHO_API_TOKEN`` or ``~/.config/byoi/utho.token``. It
lives there rather than in ``data/secrets/`` deliberately: that directory is
copied onto the VM and lands in ``salon-backup`` archives, and this token can
create and destroy every server on the account. The VM never needs it.

Endpoints are the published v2 API (https://utho.com/api-docs/). Utho answers
an error with HTTP 200 and ``{"status": "error"}``, so every call checks the
body rather than the status code.
"""

from __future__ import annotations

import argparse
import json
import os
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

API = os.environ.get("UTHO_API", "https://api.utho.com/v2")
TOKEN_FILE = Path(os.environ.get("UTHO_TOKEN_FILE", Path.home() / ".config/byoi/utho.token"))

CONSOLE_TOKENS = "https://console.utho.com/switch/api"
DESTROY_CONFIRM = "I am aware this action will delete data and server permanently"


class UthoError(Exception):
    pass


# --- transport ----------------------------------------------------------------


def token() -> str:
    env = os.environ.get("UTHO_API_TOKEN")
    if env:
        return env.strip()
    if TOKEN_FILE.is_file() and TOKEN_FILE.stat().st_size:
        return TOKEN_FILE.read_text().strip()
    raise UthoError(
        f"no Utho token.\n\n"
        f"Generate a Personal Access Token at {CONSOLE_TOKENS}, then:\n"
        f"  mkdir -p {TOKEN_FILE.parent} && chmod 700 {TOKEN_FILE.parent}\n"
        f"  printf %s '<token>' > {TOKEN_FILE} && chmod 600 {TOKEN_FILE}"
    )


def api(method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{API}/{path}", data=data, method=method)
    req.add_header("Authorization", f"Bearer {token()}")
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60, context=ssl.create_default_context()) as r:
            raw = r.read().decode()
    except urllib.error.HTTPError as exc:  # Utho still puts the reason in the body
        raw = exc.read().decode()
    except urllib.error.URLError as exc:
        raise UthoError(f"cannot reach {API}: {exc.reason}") from exc

    try:
        parsed = json.loads(raw)
    except ValueError:
        raise UthoError(f"unreadable response from {path}: {raw[:400]}") from None
    if isinstance(parsed, dict) and parsed.get("status") not in (None, "success"):
        raise UthoError(parsed.get("message") or json.dumps(parsed)[:400])
    return parsed


def rows(payload) -> list[dict]:
    """Utho nests its lists under a different key per endpoint.

    Take the first list of objects rather than hard-coding a name per call.
    """
    if isinstance(payload, list):
        return payload
    for value in payload.values():
        if isinstance(value, list) and (not value or isinstance(value[0], dict)):
            return value
    return []


def num(value) -> float:
    try:
        return float(str(value).strip() or 0)
    except ValueError:
        return 0.0


# --- resolving what a person typed --------------------------------------------


def find_server(target: str) -> dict:
    """Accept a cloudid or the hostname the VM was created with.

    Hostname is what an operator remembers, because it is the salon domain.
    """
    for row in rows(api("GET", "cloud")):
        if str(row.get("cloudid")) == target or row.get("hostname") == target:
            return row
    raise UthoError(f"no Utho server with cloudid or hostname {target!r} — ./utho.py list")


def public_ip(server: dict) -> str:
    return server.get("ip") or (server.get("v4") or {}).get("ip_address") or ""


def read_server(cloudid: str) -> dict:
    payload = api("GET", f"cloud/{cloudid}")
    found = rows(payload)
    if not found:
        raise UthoError(f"server {cloudid} not found")
    return found[0]


# --- commands -----------------------------------------------------------------


def cmd_check(args) -> None:
    user = api("GET", "account/info")
    user = user.get("user") or user
    for key in ("email", "fullname", "company", "currency", "credit", "availablecredit"):
        if user.get(key) not in (None, ""):
            print(f"{key:18} {user[key]}")


def cmd_zones(args) -> None:
    for row in sorted(rows(api("GET", "dczones")), key=lambda r: str(r.get("slug", ""))):
        city = row.get("city") or row.get("name") or ""
        country = row.get("country") or row.get("dccc") or ""
        print(f"{row.get('slug', ''):24} {city:20} {country}")


def cmd_plans(args) -> None:
    found = rows(api("GET", "plans"))
    found = [r for r in found if str(r.get("type", "cloud")).lower() in ("cloud", "")]
    found = [r for r in found if num(r.get("ram")) >= args.min_ram * 1024]
    if not found:
        raise UthoError(f"no cloud plan with at least {args.min_ram}g of RAM")
    print(f"{'planid':<10} {'ram':>5} {'cpu':>4} {'disk':>6} {'bandwidth':>10} {'monthly':>9}  slug")
    for row in sorted(found, key=lambda r: (num(r.get("ram")), num(r.get("cpu")))):
        monthly = num(row.get("monthly") or row.get("price"))
        print(
            f"{str(row.get('id', '')):<10} {num(row.get('ram')) / 1024:>4.0f}g "
            f"{str(row.get('cpu', '')):>4} {str(row.get('disk', '')):>5}g "
            f"{str(row.get('bandwidth', '')):>10} {monthly:>9.2f}  {row.get('slug', '')}"
        )


def cmd_images(args) -> None:
    match = args.match.lower()
    for row in rows(api("GET", "cloud/images")):
        name = row.get("image", "")
        haystack = f"{name} {row.get('distribution', '')}".lower()
        if match in haystack:
            print(f"{name:34} {row.get('distribution', ''):14} {row.get('version', '')}")


def cmd_keys(args) -> None:
    for row in rows(api("GET", "key")):
        print(f"{str(row.get('id', '')):<10} {row.get('name', ''):26} {row.get('type', '')}")


def cmd_key_import(args) -> None:
    path = Path(args.pubkey).expanduser()
    if not path.is_file():
        raise UthoError(f"no public key at {path}")
    key = path.read_text().strip()
    if not key.startswith(("ssh-", "ecdsa-")):
        raise UthoError(f"{path} is not an OpenSSH public key — pass the .pub, not the private key")
    result = api("POST", "key/import", {"name": args.name, "sshkey": key})
    print(json.dumps(result, indent=2))
    print("\nPass the id above as UTHO_SSHKEYS when you create the VM.")


def cmd_create(args) -> None:
    hostname = args.hostname
    # Refuse a second VM with the same hostname: otherwise ip/destroy become
    # ambiguous, and that is the wrong thing to discover during a teardown.
    if any(r.get("hostname") == hostname for r in rows(api("GET", "cloud"))):
        raise UthoError(f"a Utho server named {hostname} already exists — ./utho.py show {hostname}")

    missing = [k for k in ("UTHO_DCSLUG", "UTHO_PLANID") if not os.environ.get(k)]
    if missing:
        raise UthoError(
            f"set {' and '.join(missing)} first — ./utho.py zones, ./utho.py plans --min-ram 16"
        )

    # The Go SDK posts cloud:[{hostname}]; the published apidoc says hostname.
    # Send both — which one the backend reads is not worth a failed deploy.
    body = {
        "dcslug": os.environ["UTHO_DCSLUG"],
        "image": os.environ.get("UTHO_IMAGE", "ubuntu-24.04-x86_64"),
        "planid": os.environ["UTHO_PLANID"],
        "hostname": hostname,
        "cloud": [{"hostname": hostname}],
        "enable_publicip": "true",
        # The salon runs monthly: it is a standing venue, not a burst of work,
        # and the box stays up between Saturdays. Utho also takes hourly,
        # 3month, 6month, and 12month.
        "billingcycle": os.environ.get("UTHO_BILLINGCYCLE", "monthly"),
        "cpumodel": os.environ.get("UTHO_CPUMODEL", "amd"),
    }
    for env, field in (
        ("UTHO_SSHKEYS", "sshkeys"),
        ("UTHO_FIREWALL", "firewall"),
        ("UTHO_VPC", "vpc"),
        ("UTHO_ROOT_PASSWORD", "root_password"),
    ):
        if os.environ.get(env):
            body[field] = os.environ[env]
    if "sshkeys" not in body and "root_password" not in body:
        raise UthoError(
            "no way to log in would exist.\n"
            "  ./utho.py key-import salon ~/.ssh/id_ed25519.pub   then set UTHO_SSHKEYS=<id>\n"
            "  or set UTHO_ROOT_PASSWORD to a password you generated"
        )

    created = api("POST", "cloud/deploy", body)
    print(f"cloudid: {created.get('cloudid')}")
    print(f"ipv4:    {created.get('ipv4') or f'(pending — ./utho.py wait {hostname})'}")
    print(f"billing: {body['billingcycle']}")
    if created.get("password"):
        print(f"root password: {created['password']}")
        print("  Shown once. Utho will not repeat it, and this script does not store it.")


def cmd_list(args) -> None:
    print(f"{'cloudid':<10} {'hostname':<34} {'ip':<16} {'power':<9} {'ram':>7} cpu")
    for row in rows(api("GET", "cloud")):
        print(
            f"{str(row.get('cloudid', '')):<10} {row.get('hostname', ''):<34} "
            f"{public_ip(row):<16} {row.get('powerstatus', ''):<9} "
            f"{str(row.get('ram', '')):>7} {row.get('cpu', '')}"
        )


def cmd_show(args) -> None:
    server = read_server(str(find_server(args.target)["cloudid"]))
    for key in (
        "cloudid", "hostname", "ip", "powerstatus", "status", "cpu", "ram",
        "disksize", "billingcycle", "cost", "hourlycost", "nextinvoiceamount",
        "nextduedate", "created_at",
    ):
        if server.get(key) not in (None, ""):
            print(f"{key:18} {server[key]}")
    location = server.get("dclocation") or {}
    if location:
        print(f"{'location':18} {location.get('dc') or location.get('location') or location}")


def cmd_ip(args) -> None:
    ip = public_ip(read_server(str(find_server(args.target)["cloudid"])))
    if not ip:
        raise UthoError("no public IPv4 assigned yet")
    print(ip)


def cmd_wait(args) -> None:
    cloudid = str(find_server(args.target)["cloudid"])
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        server = read_server(cloudid)
        ip = public_ip(server)
        if ip and str(server.get("powerstatus", "")).lower() == "running":
            # Running is the hypervisor's opinion. What the next step needs is
            # sshd, which answers a while later.
            probe = subprocess.run(
                ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
                 "-o", "ConnectTimeout=5", f"root@{ip}", "true"],
                capture_output=True,
            )
            if probe.returncode == 0:
                print(ip)
                return
        time.sleep(10)
    raise UthoError(f"{args.target} was not reachable within {args.timeout}s — ./utho.py show {args.target}")


def cmd_destroy(args) -> None:
    server = find_server(args.target)
    cloudid = str(server["cloudid"])
    cmd_show(argparse.Namespace(target=cloudid))
    print()
    print("This deletes the server and its disk. The salon CA, the operator hash,")
    print("and any Claude accounts logged in on it go with it. Back up first:")
    print(f"  ssh root@{public_ip(server)} 'cd /opt/byoi && ./scripts/salon-backup.sh'")
    print()
    typed = input(f"Type {server.get('hostname') or cloudid} to confirm: ").strip()
    if typed not in (server.get("hostname"), cloudid):
        raise UthoError("not confirmed; nothing destroyed")
    result = api("DELETE", f"cloud/{cloudid}/destroy", {"confirm": DESTROY_CONFIRM})
    print(result.get("message", "destroyed"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="utho.py",
        description=__doc__.split("\n\n")[1],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("check", help="token works, and what the account is spending").set_defaults(fn=cmd_check)
    sub.add_parser("zones", help="dcslug values").set_defaults(fn=cmd_zones)

    plans = sub.add_parser("plans", help="planid values")
    plans.add_argument("--min-ram", type=float, default=0, metavar="GB")
    plans.set_defaults(fn=cmd_plans)

    images = sub.add_parser("images", help="image values")
    images.add_argument("match", nargs="?", default="ubuntu")
    images.set_defaults(fn=cmd_images)

    sub.add_parser("keys", help="SSH keys on the account").set_defaults(fn=cmd_keys)

    key_import = sub.add_parser("key-import", help="upload an OpenSSH public key")
    key_import.add_argument("name")
    key_import.add_argument("pubkey")
    key_import.set_defaults(fn=cmd_key_import)

    create = sub.add_parser("create", help="deploy one VM (this costs money)")
    create.add_argument("hostname", help="use the salon domain — other commands look it up by name")
    create.set_defaults(fn=cmd_create)

    sub.add_parser("list", help="servers on the account").set_defaults(fn=cmd_list)

    for name, fn, helptext in (
        ("show", cmd_show, "one server in detail"),
        ("ip", cmd_ip, "its public IPv4, alone"),
        ("destroy", cmd_destroy, "delete the server and its disk"),
    ):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("target", help="hostname or cloudid")
        p.set_defaults(fn=fn)

    wait = sub.add_parser("wait", help="block until Running and sshd answers")
    wait.add_argument("target", help="hostname or cloudid")
    wait.add_argument("--timeout", type=int, default=int(os.environ.get("UTHO_WAIT_TIMEOUT", "600")))
    wait.set_defaults(fn=cmd_wait)

    args = parser.parse_args(argv)
    try:
        args.fn(args)
    except UthoError as exc:
        print(f"utho: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
