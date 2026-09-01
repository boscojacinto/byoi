---
name: salon-local
description: Run the salon on one PC in static mode — TLS, the desk on :8080, the seat on :8787, the guest phone on the same Wi-Fi. Use when asked to run/start/bring up the salon locally, to open a salon on this machine, to get a phone connected to a seat, or to check why a local check-in is failing.
---

# Running the salon on one PC

`static` is the default shape and the original one: a seat is **this PC**, not a
container the desk raises. `BYOI_SEATS` picks between the two, and everything
below assumes it is unset. The cloud shape is [`deploy-utho`](../deploy-utho/SKILL.md);
this is the other column of the table at [`docs/salon.md:9`](../../../docs/salon.md).

| | here (`static`) | cloud (`ondemand`) |
|---|---|---|
| A seat is | this Linux PC | a container per check-in |
| The phone | must join this PC's Wi-Fi | anywhere, incl. cellular |
| Guest TLS | this seat, salon CA | Caddy, real certificate |
| Phone installs `ca.pem` | **yes** | no |
| The PeriPage | this PC's Bluetooth | at the counter, via relay |

`check-accounts.sh` sits next to this file: it answers the one question the
salon will not answer for you, which is whether the Claude account pool actually
has credentials in it.

## Ports

| Port | What | Who reaches it |
|---|---|---|
| 8080 | desk | operator, on this PC |
| 8786 | seat, plain HTTP | this PC's browser (no CA needed) |
| 8787 | seat, guest HTTPS | the phone, over Wi-Fi |
| 8788 | seat control, mTLS | the desk only |
| 3000 | the guest's dev server | their Claude's browser, and their phone |

## First time on this PC

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[salon,dev]"
./scripts/salon-tls.sh                 # salon CA, mTLS certs, host token
./scripts/salon-secrets.sh operator    # desk sign-in password
```

The headless browser a guest's Claude uses to look at the page it is building
(`deploy/seat-mcp.json`). Optional — without it a seat still opens, the guest
just loses the page snapshot — but a brief with a screen is most of them:

```bash
npm install -g @playwright/mcp@0.0.80
playwright-mcp install-browser chromium --with-deps
```

Install the browser with that package's own CLI, not a separately installed
`playwright`: browser builds are pinned per Playwright version, and a mismatch
fails at the first navigate rather than here.

Then the Claude accounts. **Two at minimum** — with one, a usage limit ends the
visit instead of moving it to a spare chair:

```bash
./scripts/seat-claude-login.sh --account claude-seat-1
CLAUDE_CONFIG_DIR=data/claude-accounts/claude-seat-1 claude auth login --claudeai

./scripts/seat-claude-login.sh --account claude-seat-2
CLAUDE_CONFIG_DIR=data/claude-accounts/claude-seat-2 claude auth login --claudeai

./scripts/seat-claude-login.sh --account claude-host   # grades suites; never guest chat
CLAUDE_CONFIG_DIR=data/claude-accounts/claude-host claude auth login --claudeai
```

Confirm before trusting it:

```bash
.claude/skills/salon-local/check-accounts.sh
```

**Not `claude setup-token`.** It prints a token for you to export and writes
nothing into `CLAUDE_CONFIG_DIR`, so the account looks logged in to you and empty
to the pool. The pool skips a credential-less dir silently — that is exactly the
failure `check-accounts.sh` exists to catch.

## Bring it up

Two terminals, both with the venv active:

```bash
./scripts/run-salon.sh    # desk :8080
./scripts/run-seat.sh     # seat :8787 (+ :8786 HTTP copy, :8788 control)
```

Then check the floor is actually reachable:

```bash
./scripts/wifi-status.sh
```

It prints this PC's LAN IP and confirms all three ports are listening. `MISS` on
:8788 is the one that matters most — check-in **fails** if the seat's control
port will not accept the desk's client certificate.

| Open | URL |
|---|---|
| Desk | <http://127.0.0.1:8080/> |
| Seat | <http://127.0.0.1:8786/> |
| Guest (phone) | `https://<lan-ip>:8787/guest/` |

## Getting the phone on

1. Put the phone on the **same Wi-Fi** as this PC. There is no public hostname
   here; a phone on cellular cannot reach this seat.
2. The seat's certificate is signed by the salon CA, so Safari and Chrome warn
   until the guest installs it from `https://<lan-ip>:8787/ca.pem`.
3. Check the coder in at the desk. It POSTs the OTP to the seat over mTLS —
   never over the guest's Wi-Fi — and prints a slip whose QR is
   `https://<lan-ip>:8787/join?otp=<otp>`.
4. Open the QR, claim a brief, **Open chat**.

**If DHCP moved this PC, re-run `./scripts/salon-tls.sh`.** The seat certificate
lists the current LAN IPs in its SAN; on a new IP the phone gets a name mismatch
that no amount of trusting the CA will fix. The CA itself survives the reissue,
so a phone that already trusts it stays trusting.

## Briefs, grading, and Docker

Docker is still needed here, just not for the seat:

* A brief with a stack gets its Postgres and Redis from `docker compose` on this
  PC. The app reads `DATABASE_URL`, `REDIS_URL`, and `AUTH_SECRET` and branches
  on nothing — same contract as the cloud.
* Grading runs the generated suite in a container with `--network none` and a
  scrubbed environment, under the `claude-host` account. The suite is written
  from the spec **before** anything reads the guest's code.

## Printing

Local is the default (`BYOI_PRINT_MODE` unset), so the desk drives the PeriPage
over this PC's own Bluetooth:

```bash
export PERIPAGE_MAC=C6:6C:09:0B:B2:50
```

Unset, the desk dumps the protocol instead of printing — fine for a dry run. The
QR is on the desk screen either way, so a dead printer delays paper, not a visit.

## Desk and seat on different machines

Still `static`, just two boxes. Copy from `data/tls/`: `ca.pem host.pem
host-key.pem host.token` to the desk, `ca.pem seat.pem seat-key.pem host.token`
to the seat. Then, on the desk:

```bash
export BYOI_SEAT_CONTROL_URL=https://<seat-lan-ip>:8788
```

Identity is the certificate, not the address, so a DHCP change means updating
this variable — not reissuing certs.

## Shutting down

Ctrl-C both scripts. Nothing here is a daemon and nothing survives a reboot.
`data/` holds the salon CA key, the operator hash, and the live Claude
credentials, none of which are in git and none of which can be recreated:

```bash
./scripts/salon-backup.sh
```

## Things that bite

* **`run-seat.sh` deliberately does not load `.env`.** The guest's Claude
  inherits the seat's environment, so deploy credentials must stay off it.
  `run-salon.sh` does load it. If you "fixed" the asymmetry, put it back.
* **One Claude account is not enough.** A hard usage limit with no spare is an
  error on the guest's phone, mid-visit.
* **The 80% early switch does not fire.** `statusLine` is never invoked under
  `-p --output-format stream-json`, which is how the seat runs Claude, so
  `last-usage.json` is never written. The *hard* limit path still works —
  `parse_limit_error` reads the error off the stream. Do not spend an afternoon
  debugging the graceful path expecting it to work.
* **Two Claude Code processes must never share one `CLAUDE_CONFIG_DIR`.** They
  tread on each other, and it would let one session read credentials another is
  sitting on.
* **Credential isolation is Linux-only** — `CLAUDE_CONFIG_DIR` plus
  `.credentials.json`.
* **Expo Go cannot trust a private CA.** If you still ship the guest APK, build
  it: `cd apps/guest && npx expo run:android`.
* **`BYOI_SEAT_CONTROL_URL` belongs here and not in the cloud.** One address is
  right with one seat agent and wrong with one per session.
