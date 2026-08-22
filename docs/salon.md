# BYOI salon (coding + wellness)

Each **seat** is a Linux PC running Claude Code. The guest phone and that PC
are on the **same Wi-Fi**. Guests never log into Claude. The seat operator
runs `claude setup-token` once; guests open the **BYOI Guest** PWA and chat
after an **OTP** the host printed on the slip.

This is a phone chat, like the Claude Code mobile app — not a terminal mirror.

```
Host desk  --mTLS + token-->  Seat control :8788  (admit / revoke OTP)
Phone PWA  --HTTPS + OTP--->  Seat guest   :8787  --ticket-->  Claude Code chat
```

## Trust (host ↔ seat)

Guest Wi-Fi HTTP is **not** how the OTP is delivered. Check-in pushes the OTP
on a second port with **mutual TLS**:

| Piece | Role |
|---|---|
| Salon CA (`ca.pem`) | Both machines trust only this CA |
| Seat **server** cert | Desk knows it is talking to the seat, even if the seat's LAN IP changed |
| Host **client** cert | Seat knows it is the desk, even if the desk's LAN IP changed |
| `host.token` | Non-default shared secret (defense in depth) |

IP allowlisting (`BYOI_HOST_IPS`) is optional and off by default because cafe
DHCP moves. The **certificate** is the identity.

```bash
./scripts/salon-tls.sh
```

On **one PC** (host + seat together) you do not paste a token. After TLS:

```bash
./scripts/run-salon.sh    # :8080
./scripts/run-seat.sh     # :8787 guests + :8788 mTLS
```

Open the desk at **http://127.0.0.1:8080/** (loopback, not the LAN IP). Check-in
is allowed from this machine; phones on cafe Wi-Fi cannot use the desk page.

Two machines: export `BYOI_TLS_DIR`, `BYOI_HOST_TOKEN_FILE`, and
`BYOI_SEAT_CONTROL_URL=https://<seat-ip>:8788`.

Keep `ca-key.pem` off the cafe LAN.

Guests use **HTTPS** on `:8787` with the same salon CA. The seat certificate
lists the current LAN IPs in SAN so `https://<seat-ip>:8787` verifies. If
DHCP moves the seat, re-run `./scripts/salon-tls.sh` (CA stays; seat cert is
reissued). Safari/Chrome will warn until the guest installs `ca.pem`
(served at `/ca.pem`). The optional guest APK trusts
`apps/guest/assets/ca.pem` (copied by that script). **Expo Go cannot trust a
private CA** — use `cd apps/guest && npx expo run:android` if you still ship
the APK.

## Claude login (seat PC, once)

```bash
./scripts/seat-claude-login.sh --account claude-seat-1
CLAUDE_CONFIG_DIR=data/claude-accounts/claude-seat-1 claude setup-token

./scripts/seat-claude-login.sh --account claude-seat-2
CLAUDE_CONFIG_DIR=data/claude-accounts/claude-seat-2 claude setup-token
```

Each dir is a separate Anthropic login (`CLAUDE_CONFIG_DIR`, Linux). The guest
stays on this seat’s URL. When the live login’s 5-hour or 7-day usage hits 80%,
the seat runs `/compact`, saves that summary, and continues on the next spare
account. Guest `/handoff` (and the desk **Handoff** button) downloads the
markdown. `/export` is still the phone transcript.

Without two credentialed dirs, a hard usage limit is an error on the phone —
the visit is not moved to another chair.

The desk needs its own `claude-host` login as well — see *Grading a shipped
solution* below. It is never used for guest chat.

The seat talks to Claude Code over **stream-json** (the same machine protocol
the Agent SDK uses). Guests never see a TTY. The working directory is this
repo (override with `BYOI_WORKSPACE`). Extra trees: `BYOI_ADD_DIR=/path:/other`.

## Run

```bash
./scripts/salon-tls.sh
./scripts/run-salon.sh    # :8080  (reads data/tls/host.token)
./scripts/run-seat.sh     # :8787 guests + :8788 mTLS control
./scripts/wifi-status.sh
```

Check-in **fails** if the seat control port does not accept the desk's client
cert. Set `BYOI_SEAT_CONTROL_URL` to the seat's current IP when the machines
are separate; you do not reissue certs when DHCP changes.

## Projects on the solution board

Each brief can point at a **git project**. When the guest claims it, the seat
Claude session's working directory becomes that folder (not the empty
`data/workspace` sandbox).

On the desk (`http://127.0.0.1:8080/`):

1. **New project** — create a GitHub repo (`gh` must be logged in on this PC),
   clone an existing URL, or attach a local folder.
2. **Publish** a brief with that project selected, or assign a project on an
   existing brief.
3. Guest claims the brief → seat `cwd` switches to `project.local_path`.

Each brief can include an **acceptance spec**. When the guest marks shipped, the
phone shows passing and failing cases — one per requirement in the spec.

## Grading a shipped solution

The spec is graded by the **host** account, not the seat's. The seat only hands
over the guest's work.

```
guest taps "I'm done"
  -> desk POSTs /local/submit to the seat (mTLS)
       seat injects a sentinel prompt into the live chat
       -> UserPromptSubmit hook byoi-submit.sh records the submission
       -> seat pins the tree to refs/byoi/submissions/<session>
  -> desk generates a suite from the spec, blind
  -> desk runs it in a container against that ref
  -> desk grades it and the phone polls the result
```

Three properties this buys:

* **The author never sees the solution.** Generation runs with `--allowedTools ""`,
  so the suite is written from the spec alone and cannot be shaped by the code it
  judges.
* **Completeness is checked.** Every requirement in the spec must map to a test
  node. A requirement with no result in the JUnit report is reported as a *failed*
  `completeness:` case, so a suite that quietly skips one cannot score 100%.
* **The guest's tree is never touched.** The submission is committed through a
  scratch index onto a ref under `refs/byoi/`. No branch tracks it; the guest's
  `HEAD`, index, and working tree are unchanged.

Log the host account in once, alongside the seat accounts:

```bash
./scripts/seat-claude-login.sh --account claude-host
CLAUDE_CONFIG_DIR=data/claude-accounts/claude-host claude setup-token
```

On one PC the desk fetches the ref straight off the project folder. On two
machines the seat pushes it to the project's `origin` first, so that path needs
`gh auth login` (or a git credential helper) on the seat.

The suite runs in **Docker** with `--network none`, a pids/memory/CPU cap, and an
environment scrubbed of every `BYOI_*` value — guest code never sees `host.token`
or `data/tls/`. Runs land in `data/verify-runs/<session>/`.

| Env | Default | Meaning |
|---|---|---|
| `BYOI_HOST_CLAUDE_ACCOUNT` | `claude-host` | Account dir that writes the suite |
| `BYOI_TESTGEN_RUNTIME` | auto | `docker`, `bwrap`, or `none` |
| `BYOI_TESTGEN_IMAGE` | from the suite | Override the container image |
| `BYOI_TESTGEN_TIMEOUT` | `300` | Seconds for generation and for the run |
| `BYOI_VERIFY_RUNS_DIR` | `data/verify-runs` | Where checkouts and reports land |

If the host account is not logged in, the project is not a git repo, or no
container runtime is available, the desk falls back to the old seat-side verifier
(`apps/seat/verify.py`) and says so in the report summary.

New GitHub repos clone into `data/projects/` (override with `BYOI_PROJECTS_DIR`).
`gh auth login` once on the seat/desk PC.

## Floor

1. Host checks the coder in. Desk **POSTs the OTP to the seat over mTLS**,
   then prints a slip. QR: `https://<seat-lan-ip>:8787/join?otp=<otp>`.
2. Phone joins the same Wi-Fi as the seat PC.
3. Open the QR (or **BYOI Guest** → **Scan slip QR**). The seat serves an
   installable PWA at `/guest/`.
4. Claim a brief, then **Open chat**. Seat checks OTP, issues a ticket for
   `/chat`, and the phone is a Claude Code session: messages, tool cards,
   diffs, todos, plan/code/auto/ask modes, slash commands (`/commit`,
   `/review`, `/model`, `/compact`, …), file mentions, photos, and stop.

Add the PWA to the Home Screen for a full-screen chat next visit.

## Optional SSH / TTY

SSH and `/tty` are operator side doors onto tmux. They are **not** the guest
path and SSH is **not** OTP-gated.

```bash
sudo ./scripts/seat-guest-ssh.sh
ssh guest@<seat-lan-ip>
```
