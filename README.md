# peripage-a6

Userspace Bluetooth driver for the **PeriPage A6 304dpi** pocket thermal
printer. Python library plus a `peripage` CLI.

The 304dpi A6 (sometimes labelled A6+ / `PeriPage+XXXX_BLE`) is not an
ESC/POS printer. Current units talk a proprietary session over **Bluetooth
LE**, and print a 576-pixel-wide 1-bit raster. This tree implements that
protocol. It does **not** target the older 203dpi A6 (384 px).

## Printer

| | |
|---|---|
| Model | PeriPage A6 304dpi / A6+ |
| Resolution | 304 dpi, 576 px wide (72 bytes/row) |
| Paper | 58 mm roll, ~48.5 mm printable |
| Transport | Bluetooth LE (gatttool). Not classic RFCOMM. |
| USB | not in this first cut |

## Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Needs BlueZ, including `gatttool` (usually the `bluez` package). Do **not**
install PyBluez.

## Connect the printer

Do **not** run `bluetoothctl pair`. On this 304dpi BLE model that fails with
`org.bluez.Error.AuthenticationFailed`, and BlueZ then tries classic RFCOMM
instead of LE.

1. Charge it (this unit reported 8% — it will drop the LE link when empty).
2. Power it on (green LED). Keep the official app off the printer.
3. Print. The driver opens an LE session itself:

```bash
peripage info C6:6C:09:0B:B2:50
```

Advertised name: `PeriPage+B250` / `PeriPage+B250_BLE`. No PIN.

## CLI

```bash
peripage discover
peripage discover --scan 8

peripage info AA:BB:CC:DD:EE:FF

peripage print AA:BB:CC:DD:EE:FF photo.png
peripage print AA:BB:CC:DD:EE:FF photo.png --dither atkinson --concentration dark
peripage print AA:BB:CC:DD:EE:FF --text "grocery list"
peripage print AA:BB:CC:DD:EE:FF --qr "https://example.com"
peripage print AA:BB:CC:DD:EE:FF --ascii "built-in font, 48 columns"

peripage feed AA:BB:CC:DD:EE:FF --dots 80
```

`--dump FILE` writes the protocol stream without opening Bluetooth. Useful
for tests and for inspecting a job:

```bash
peripage print 00:00:00:00:00:00 photo.png --dump /tmp/job.bin
xxd /tmp/job.bin | head
```

Dither options: `floyd-steinberg` (default, photos), `atkinson` (slightly
lighter), `threshold` (text / line art). Concentration: `light`, `medium`,
`dark`. Dark lasts longer on the paper and runs the head hotter.

## Library

```python
from peripage_a6 import Printer, Concentration, Dither, open_ble_transport

with Printer(open_ble_transport("C6:6C:09:0B:B2:50")) as printer:
    print(printer.info())
    printer.print_image("photo.png", dither=Dither.ATKINSON, concentration=Concentration.DARK)
    printer.print_text("hello from Python")
    printer.print_qr("https://example.com")
```

Swap the transport for `DumpTransport("job.bin")` to capture bytes. `--transport bleak`
uses BlueZ GATT via bleak (usually fails on this dual-mode firmware).
`--transport rfcomm` is only for older non-`_BLE` A6 units.

## How a job is sent

1. BLE GATT connect (write `0000ff02`, notify `0000ff01`), then vendor reset (`10 FF FE 01` + 12 zeros).
2. Set concentration.
3. `GS v 0` raster, 72 bytes/row, at most 255 rows per command.
4. `ESC J` paper feed, then vendor end-of-job (`10 FF FE 45`).

Writes are paced (~two rows / 15 ms) so the Bluetooth buffer does not
outrun the head. Details: [`docs/protocol.md`](docs/protocol.md).

## Tests

No hardware required:

```bash
pytest
```

## Salon (coding + wellness)

Cafe floor: a **desk** checks guests in and prints a slip, a **seat PC** runs
Claude Code, and the **guest phone** is a chat PWA (not a terminal). Same
Wi-Fi. Guests never log into Claude. Full notes: [`docs/salon.md`](docs/salon.md).

```
Desk  :8080   check-in, floor, solution board
Seat  :8786   HTTP UI on this PC (no cert warning)
Seat  :8787   HTTPS guest PWA for phones
Seat  :8788   mTLS control (desk → seat)
```

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[salon,dev]"
./scripts/salon-tls.sh
./scripts/seat-claude-login.sh --account claude-seat-1   # then: claude setup-token
./scripts/run-salon.sh           # desk
./scripts/run-seat.sh            # seat UI + guest PWA
```

| Open on this PC | URL |
|---|---|
| Desk | http://127.0.0.1:8080/ |
| Seat | http://127.0.0.1:8786/ |
| Guest (phone) | `https://<seat-lan-ip>:8787/guest/` |

Desk check-in is allowed from **127.0.0.1** on this machine (no token paste).
Phones cannot use the desk page. The slip QR is `https://<seat-ip>:8787/join?otp=…`.

**Floor / Solutions / Live.** Desk tabs: seats, the solution board, and a
mirror of the guest session. **Sit a guest** opens a centered QR — the
image is the join code only; the printer still gets the full thermal slip.
On the phone: same Wi-Fi, scan, pick a solution, **Chat**. Claude Code runs
on the seat; the phone is messages, tools, diffs, files, photos, plan/code
modes, and a session timer.

**Your own Claude account.** A guest who already pays for Claude can tap **Use my
own Claude account** and run the session on theirs. They sign in on their own
phone — their password never reaches the seat — and the token that does is kept on
tmpfs and revoked and deleted when the seat is freed. It is a full-access Claude
Code token, not a narrowed one: the phone names what it can do before the guest
approves. It is also not private from the operator while the session runs.

**Solutions.** Each board item can have a **project** (new GitHub repo, clone,
or a folder on this PC). Claiming it sets the seat workspace to that folder.
An optional **acceptance spec** runs when the guest taps **I'm done** — the
seat grades the work and the phone shows pass/fail.

The board a fresh desk opens with is `apps/api/seed_board.py` — today, the
fixes waiting on [The Fusion Studio](https://github.com/boscojacinto/thefusionstudio)
site. That repo is cloned on the first claim (or from the desk's **Fetch repo**
button), so startup stays offline.

`gh auth login` once if you create GitHub repos from the desk. New clones
land in `data/projects/`. Phone browsers warn on the salon CA until
`https://<seat-ip>:8787/ca.pem` is installed.

## What this is not

- Not a CUPS driver. The library is the core a CUPS filter can sit on later.
- Not USB. Same raster commands, different pipe; left for a follow-up.
- Not the 203dpi A6. That head is 384 px / 48 bytes/row.

## Hardware notes

The head overheats on long solid-black jobs and silently drops rows (internal
buffer is only a few hundred pixels high). Pause between large pages. The
official app is still the only documented way to update firmware.
