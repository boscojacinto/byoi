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

## A guest's own Claude account

A guest who already pays for Claude can run the session on their account rather
than the salon's — **Your Claude account** on the floor screen. Nothing is set up
in advance and the host does nothing.

```
phone: "Use my own Claude account"
  seat mints /run/user/<uid>/byoi/guest-<session>/   tmpfs, 0700
  seat runs `claude auth login` there, in a pty
  phone shows the OAuth link -> guest signs in on THEIR phone, at claude.ai
  guest pastes the code back -> .credentials.json lands on tmpfs
free the seat -> `claude auth logout`, then unlink
```

What this buys, and what it does not:

* The guest's **password and 2FA never touch the seat**. They authenticate on
  their own phone, on claude.ai, where their own password manager already has an
  entry. The seat only ever sees the resulting token.
* That token is scoped to **inference alone** — `user:inference`, no
  `user:profile`, no `org:create_api_key`. Widen it with
  `BYOI_GUEST_OAUTH_SCOPES` if quota reporting needs more.
* It lives on **tmpfs**, so it never reaches the SSD. Check
  `swapon --show` is empty (or encrypted) or that guarantee is only partial —
  the seat says so in the start response when it is not.
* Teardown **revokes** before it unlinks. Deleting `.credentials.json` alone
  would leave a live refresh token valid until `refreshTokenExpiresAt`.
* It is **not** private from the operator during the visit. Claude Code runs on
  the seat, so root there can read the live token. The phone says this in as many
  words before the guest signs in.

Freeing the seat also removes `data/handoffs/<session>.md`. The guest's project
tree, its git history, and any `refs/byoi/` submission are left alone — those are
their work, not their credentials.

A BYO session **never fails over to a salon account**: hitting the guest's own
usage limit reports that limit and stops, rather than quietly moving their work
onto the salon's billing. The host can still switch deliberately from the desk.

| Env | Default | Meaning |
|---|---|---|
| `BYOI_GUEST_OAUTH_SCOPES` | `user:inference` | Scopes asked for at guest sign-in |
| `BYOI_GUEST_RUNTIME_DIR` | `$XDG_RUNTIME_DIR` or `/run/user/<uid>` | Where the ephemeral account lives |

Without a tmpfs runtime dir the seat falls back to `data/guest-accounts/` and
overwrites before unlinking — best effort only on a journaling filesystem, and
reported rather than hidden.

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
machines the seat pushes it to the project's `origin` first, so the seat needs
to be able to push there:

| Project `origin` | What the seat needs |
|---|---|
| `git@github.com:…` (SSH) | An authorised SSH key. Nothing else. |
| `https://github.com/…` | `gh auth login` on the seat, which installs a git credential helper |

`gh` is only otherwise needed for **New project → GitHub**. If a push fails the
desk falls back to the seat-side verifier and the report says which of the two
fixes applies.

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

## Deployable projects

A brief can point at a project that has a real data layer. **New project →
Template** copies `apps/templates/next-fullstack` — Next.js with Postgres, a
session cookie, and Redis already wired — and commits it. Existing repos are
sniffed instead (`byoi.json` if present, otherwise `package.json`, lockfiles,
and `.env.example`), so any Next/Nuxt/Astro/SvelteKit tree works.

### The URL contract

The app only ever reads `DATABASE_URL`, `REDIS_URL`, and `AUTH_SECRET`:

| Where | What backs them |
|---|---|
| Seat, during the session | Docker Compose stack, written into `.env.local` |
| Vercel preview, on deploy | Managed Postgres/Redis, injected by the desk |

Nothing in the app branches on environment. The same code is developed, graded,
and deployed.

### On the seat

Claiming a brief whose project needs infrastructure starts a per-session stack
(`docker compose -p byoi-<session>`) with **ephemeral host ports**, so several
seats on one PC never collide. Only the salon's own block of `.env.local` is
rewritten — a guest's own variables survive. `docker compose down -v` runs when
the host frees the seat.

### Deploying

The guest taps **Deploy preview**. The seat pins the tree to
`refs/byoi/deploys/<session>` — the same non-destructive capture used for
grading — and the desk fetches it, provisions managed infrastructure, and runs
`vercel` with its own token.

**The token never goes near the seat.** The guest's Claude has Bash and inherits
the seat's environment, so a Vercel token there would be readable, and usable,
by guest code. The desk deploys from a checkout instead, at a moment when no
guest code is running, and passes `vercel` a scrubbed environment. Tokens are
redacted out of any error before it is stored or shown.

Vercel gates preview deployments behind its own SSO by default, which would make
the URL unopenable by the guest who built it and unreachable by the smoke suite.
The desk turns that off for the project it creates (`BYOI_VERCEL_PUBLIC=0` keeps
Vercel's default). Treat every preview as public.

If the preview comes up and the brief has a spec, a **smoke suite** is generated
from that spec and run against the live URL. That run gets network access, so it
gets *only* the generated tests — the guest's tree is never in the directory.

### Teardown

Ephemeral by policy: freeing the seat removes the deployment, **deletes the
Vercel project** so no build history lingers, destroys the provisioned database
and cache, and brings the seat's local stack down. A
failure at any step is recorded but never blocks freeing the seat.

### Credentials (desk only)

Looked up in this order, first hit wins:

1. the environment
2. `data/secrets/<name>` — what `salon-secrets.sh` writes
3. `.env` at the repo root

`data/secrets/` deliberately beats `.env`, so rotating a credential there is
never silently undone by a stale copy someone left in `.env`. Prefer either file
over an exported variable: the desk is a long-running process, so exporting in
another shell cannot reach it, and a token on a command line lands in shell
history and in `ps`.

**These are desk credentials and the seat must never see them.** The guest's
Claude has Bash and inherits the seat's environment, so `run-seat.sh` does not
load `.env`, and every seat path that spawns a process — the chat, the tmux
session, the PTY side door — scrubs them first.

```bash
./scripts/salon-secrets.sh vercel     # prompts, writes data/secrets/vercel.token 0600
./scripts/salon-secrets.sh neon
./scripts/salon-secrets.sh upstash
./scripts/salon-secrets.sh --list     # what is configured; never the values
```

| Credential | File | Effect if unset |
|---|---|---|
| `BYOI_VERCEL_TOKEN` | `vercel.token` | Deploy is refused with a clear message |
| `BYOI_VERCEL_SCOPE` | `vercel.scope` | Personal account is used |
| `BYOI_NEON_API_KEY` | `neon.token` | Ships without a managed Postgres, and says so |
| `BYOI_UPSTASH_EMAIL` | `upstash.email` | Ships without a managed Redis, and says so |
| `BYOI_UPSTASH_API_KEY` | `upstash.token` | ” |

`data/` and `.env` are both gitignored, so these never reach a commit. Point
elsewhere with `BYOI_SECRETS_DIR`, `BYOI_ENV_FILE`, or a single file with e.g.
`BYOI_VERCEL_TOKEN_FILE`.

The test suite blanks all of these, so no test can reach a live provider API
with a real token or print one into a failure message.

Auth needs no vendor: a fresh `AUTH_SECRET` is minted per deploy. Missing
credentials degrade the deploy, they never fail it — which is the right
behaviour for a brief that has no data layer.

One known limitation: `vercel` takes environment values as command-line
arguments, so during the seconds a deploy is running they are visible in `ps`
to other local users **on the desk PC**. The desk is the operator's own machine
and the guest has no account on it, so this is not a guest-facing hole — but do
not run the desk on a shared login.

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
