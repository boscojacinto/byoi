# BYOI salon (coding + wellness)

A **seat** runs Claude Code. Guests never log into Claude: the operator runs
`claude auth login` once per account, and guests open the **BYOI Guest** PWA
and chat after an **OTP** the host printed on the slip.

This is a phone chat, like the Claude Code mobile app — not a terminal mirror.

## Where this runs

Two shapes, same code. `BYOI_SEATS` picks.

| | `static` (default) | `ondemand` |
|---|---|---|
| A seat is | a Linux PC in the room | a container the desk raises at check-in |
| The guest's phone | must be on the seat's Wi-Fi | anywhere; the seat has a public hostname |
| Guest TLS | the seat, with the salon CA | Caddy, with a real certificate |
| The phone must install `ca.pem` | yes | no |
| Postgres / Redis for a brief | `docker compose` on the seat | raised by the desk, on a per-session network |
| The PeriPage | on the seat PC's Bluetooth | at the counter, fed by `scripts/print-relay.py` |
| Bring it up with | `run-salon.sh` + `run-seat.sh` | `scripts/cloud-up.sh` |

```
static:
  Host desk  --mTLS + token-->  Seat control :8788  (admit / revoke OTP)
  Phone PWA  --HTTPS + OTP--->  Seat guest   :8787  --ticket-->  Claude Code chat

ondemand:
  Phone  --https://s-<session>.<domain>-->  Caddy  -->  byoi-seat-<session>:8787
  Desk   --mTLS + token---------------->  byoi-seat-<session>:8788
  Desk   --docker------------------------>  the seat, its Postgres, its Redis
  Counter  print-relay.py  --https-->  desk print queue  --BLE-->  PeriPage A6
```

The rest of this document describes both. Where they differ it says so.

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

On **one PC** (host + seat together):

```bash
./scripts/run-salon.sh    # :8080
./scripts/run-seat.sh     # :8787 guests + :8788 mTLS
```

Two machines: export `BYOI_TLS_DIR`, `BYOI_HOST_TOKEN_FILE`, and
`BYOI_SEAT_CONTROL_URL=https://<seat-ip>:8788`.

Keep `ca-key.pem` off the cafe LAN.

### Signing in to the desk

**There is no same-machine shortcut, in either mode.** There used to be: a
request from `127.0.0.1` was taken as the operator standing at the counter. Put
a reverse proxy in front of that and every request in the world arrives from
`127.0.0.1` — the test stops identifying anybody and starts admitting everybody.
So the desk has a password:

```bash
./scripts/salon-secrets.sh operator
```

It is scrypt-hashed into `data/secrets/operator.hash` and exchanged for a signed,
`HttpOnly` cookie — 12 hours absolute, 2 hours idle, and eight wrong guesses per
address buys a 15-minute lockout. `host.token` still works as a `Bearer` header,
which is how the seat, the print relay, and anything else without a browser
authenticate.

### Guest TLS

**`static`.** The seat serves HTTPS on `:8787` with the salon CA. Its
certificate lists the current LAN IPs in SAN so `https://<seat-ip>:8787`
verifies. If DHCP moves the seat, re-run `./scripts/salon-tls.sh` (CA stays;
seat cert is reissued). Safari/Chrome warn until the guest installs `ca.pem`
(served at `/ca.pem`). The optional guest APK trusts
`apps/guest/assets/ca.pem` (copied by that script). **Expo Go cannot trust a
private CA** — use `cd apps/guest && npx expo run:android` if you still ship
the APK.

**`ondemand`.** Caddy holds a real wildcard certificate for `*.<domain>`, issued
over DNS-01, and the seat speaks plain HTTP behind it. The guest installs
nothing and no browser warns. One wildcard rather than on-demand issuance,
because a certificate per visit walks into Let's Encrypt's rate limits on a busy
Saturday. `/ca.pem` and the APK's bundled CA are legacy in this mode — the salon
CA is down to one job, below.

### The seat's own front door

`BYOI_GUEST_NET` says what network a seat will answer on. `lan` (default) admits
private addresses only, which is right when the seat is a PC in the room. `public`
**removes** that check rather than leaving it to pass for everybody: behind a
proxy the client address is the proxy's, so an address test there would look like
a control while being none. What actually gates the chat is unchanged in both
modes — the OTP, then the ticket, with the eight-failure lockout in
`apps/seat/gate.py`. Both apps run with `--proxy-headers`, so that lockout counts
the phone rather than the proxy.

In `ondemand` the salon CA no longer holds guest TLS at all. It does one thing:
prove the desk is the desk on `:8788`. Each seat gets **its own** key and a
certificate naming its container, minted at check-in and destroyed with the
visit.

## Claude login (seat PC, once)

```bash
./scripts/seat-claude-login.sh --account claude-seat-1
CLAUDE_CONFIG_DIR=data/claude-accounts/claude-seat-1 claude auth login --claudeai

./scripts/seat-claude-login.sh --account claude-seat-2
CLAUDE_CONFIG_DIR=data/claude-accounts/claude-seat-2 claude auth login --claudeai
```

Confirm each one took, because an account with no credentials is silently
skipped by the pool:

```bash
CLAUDE_CONFIG_DIR=data/claude-accounts/claude-seat-1 claude auth status
```

`"loggedIn": true` is what you want. **Not `claude setup-token`** — that prints a
token for you to export as `CLAUDE_CODE_OAUTH_TOKEN` and writes nothing into
`CLAUDE_CONFIG_DIR`, so the account looks logged in to you and empty to the
salon. Measured on Claude Code 2.1.197.

Each dir is a separate Anthropic login (`CLAUDE_CONFIG_DIR`, Linux). The guest
stays on this seat’s URL. When the live login’s 5-hour or 7-day usage hits 80%,
the seat runs `/compact`, saves that summary, and continues on the next spare
account. Guest `/handoff` (and the desk **Handoff** button) downloads the
markdown. `/export` is still the phone transcript.

Without two credentialed dirs, a hard usage limit is an error on the phone —
the visit is not moved to another chair.

In `ondemand` the desk allocates accounts to a visit and bind-mounts **only those
dirs** into the seat container. An account is never handed to two live seats at
once — two Claude Code processes on one credential directory tread on each other,
and it would also let one guest's session read the credentials another guest is
sitting on. Run out of free accounts and check-in says so rather than
double-booking one.

**Known broken: the 80% early switch.** Measured against Claude Code 2.1.241,
`statusLine` is **never invoked** under `-p --output-format stream-json`, which is
how the seat runs Claude. So `last-usage.json` is never written, `refresh_quota()`
is always None, and the compact-then-switch-at-80% path never fires. This is not
specific to a guest's own account — it affects salon accounts the same way, and it
was masked because `scripts/fake-claude.py` writes that file itself, so every test
of the feature passes against the fake. The **hard** limit path is unaffected:
`parse_limit_error` reads the error off the stream, so a real usage limit still
switches accounts (or reports `no_spare` on a BYO session). Needs its own fix —
probably reading usage from the `result` event rather than a status-line hook.

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
* **The token is not narrowed, and must never be described as though it were.**
  `claude auth login` (2.1.241) requests a fixed scope set — `org:create_api_key
  user:profile user:inference user:sessions:claude_code user:mcp_servers
  user:file_upload` — and **ignores `CLAUDE_CODE_OAUTH_SCOPES`**; setting it to a
  nonsense value produces an identical authorize URL. So the token on the seat
  can do what Claude Code normally can with that account, *including creating an
  API key on their org*. What protects the guest is that it is short-lived and
  revoked at checkout, not that it is weak. The phone names these powers before
  the guest approves, from the real `scope=` parameter rather than from what the
  seat asked for.
* It lives on **tmpfs**, so it is never written to a filesystem. On a salon PC
  that means it never reaches the SSD — check `swapon --show` is empty (or
  encrypted) or the guarantee is only partial, and the seat says so in the start
  response when it is not. **In the cloud say less than that.** The tmpfs is a
  16 MB mount inside the seat container, so the token stays out of the container
  filesystem and goes when the container does; what it is resident in is the
  VM's RAM, which belongs to a hosting provider who can snapshot it. "Never
  touches a disk we control" is true; "never touches a disk" is not.
* Teardown **revokes** before it unlinks. Deleting `.credentials.json` alone
  would leave a live refresh token valid until `refreshTokenExpiresAt`.
* It is **not** private from the operator during the visit. Claude Code runs on
  the seat, so root there can read the live token. The phone says this in as many
  words before the guest signs in. In the cloud the same sentence covers one more
  party: the seat is a container on a rented VM, so whoever runs that VM is
  inside the trust boundary too.

Freeing the seat also removes `data/handoffs/<session>.md`. The guest's project
tree, its git history, and any `refs/byoi/` submission are left alone — those are
their work, not their credentials.

A BYO session **never fails over to a salon account**: hitting the guest's own
usage limit reports that limit and stops, rather than quietly moving their work
onto the salon's billing. The host can still switch deliberately from the desk.

| Env | Default | Meaning |
|---|---|---|
| `BYOI_GUEST_OAUTH_SCOPES` | `user:inference` | Passed to the CLI, which currently ignores it for `auth login` |
| `BYOI_GUEST_RUNTIME_DIR` | `$XDG_RUNTIME_DIR` or `/run/user/<uid>` | Where the ephemeral account lives |
| `BYOI_GUEST_LOGIN_TIMEOUT` | `600` | Seconds before an abandoned sign-in is killed |

Without a tmpfs runtime dir the seat falls back to `data/guest-accounts/` and
overwrites before unlinking — best effort only on a journaling filesystem, and
reported rather than hidden.

The seat talks to Claude Code over **stream-json** (the same machine protocol
the Agent SDK uses). Guests never see a TTY. The working directory is this
repo (override with `BYOI_WORKSPACE`). Extra trees: `BYOI_ADD_DIR=/path:/other`.

### Bash approval

The seat's guest chat already has a real approval card — Claude Code sends a
`can_use_tool` control request over the same stream-json channel, the seat
turns it into a phone card (Allow / Deny), and the answer goes back the same
way. That path works for whatever the CLI actually asks about.

It does not cover everything, though: Claude Code's own Bash safety
classifier denies some commands outright in headless mode — `npm run
<script>` among them, since the script name could run anything — without
ever asking. This happens identically under every permission mode, `manual`
("ask every time") included, so there is no card to show; the CLI decided
before the seat ever saw the request. `apps/seat/claude_chat.py`'s
`DEFAULT_ALLOWED_TOOLS` pre-approves the shape of an ordinary build/test
cycle (`npm run *`, `pytest`, `git status`/`diff`/`log`, …) so a guest
following a brief doesn't hit that wall. Override it with `BYOI_CLAUDE_TOOLS`
— set to `--allowedTools`'s own syntax to replace the list, or to `""` for a
deliberately tighter guest sandbox with no default allowlist at all.

## Looking at the page

Most briefs end in something with a screen, and until a guest can see it they
are taking Claude's word for how their own work looks. Three things make that
possible, and none of them goes through Bash.

### The seat's browser

`deploy/seat-mcp.json` declares a headless Chromium as an **MCP server**, and
`claude_chat.seat_mcp_config()` passes it to `--mcp-config`. MCP is the whole
point: a browser driven from Bash would be denied by the safety classifier
above, before any card could reach the phone. MCP tools take the ordinary
permission path instead — the one the seat already turns into Allow / Deny —
and `mcp__browser` is on the default allowlist so a dozen navigate-and-look
calls do not become a dozen taps.

Claude gets `browser_navigate`, `browser_snapshot` (the accessibility tree,
which is cheaper and more useful than pixels for anything it intends to click),
`browser_take_screenshot`, `browser_console_messages` and
`browser_network_requests`. It reaches the guest's dev server at
`127.0.0.1:3000`, inside the seat.

`--strict-mcp-config` goes with it. A guest repo carrying its own `.mcp.json`
must not be able to add servers to the sandbox it is being edited in.

Two things worth knowing:

* **The browser does not narrow the seat's reach, and does not widen it
  either.** `Bash(npm run *)` is already arbitrary code with network access —
  that is why the Docker socket and the Vercel token live on the desk and not
  here. Playwright's `--allowed-origins` is left off by default because its own
  authors decline to call it a security boundary, and switching it on breaks
  every page that loads a font or a script from a CDN. An operator who wants it
  anyway adds it to `deploy/seat-mcp.json`.
* **Screenshots cost tokens**, and a seat runs on a pooled account that fails
  over on quota. `browser_snapshot` is the cheap one; pixels are for questions
  that are actually about pixels.

The image is baked with a pinned `@playwright/mcp` and installs chromium with
that same package's own CLI, because browser builds are pinned per Playwright
version. On a salon PC, install it next to Claude Code:

```bash
npm install -g @playwright/mcp@0.0.80
playwright-mcp install-browser chromium --with-deps
```

A seat with no browser installed still opens — `seat_mcp_config()` treats a
missing file as "no MCP servers", and a server that will not start costs the
guest a page snapshot, not a seat.

### The guest's own browser

The phone is the second browser, and the only one that can answer "does this
feel right on a phone". `p-<session>.<domain>` is published beside
`s-<session>` at check-in and torn down with it, pointed at port 3000 in that
seat's container. In `static` the route is unnecessary — the phone is already
on the seat's Wi-Fi, so the seat offers its own LAN address instead. Either
way the address turns up in the guest UI under **This session**.

**What this publishes is public.** The seat next door has an OTP in front of
it; a dev server does not, and could not be given one without the salon
reaching inside the guest's app. The hostname is unlisted and dies at checkout
— the same bargain this document already strikes for a Vercel preview.
`BYOI_PREVIEW_PORT=` (empty) switches it off.

The dev server has to listen on all interfaces or nothing reaches it, which is
why the template's `dev` script is `next dev -H 0.0.0.0`.

### Screenshots on the phone

A tool result carrying an image used to be collapsed to `[image]`: reading a
photo out of a repo produced hundreds of kilobytes of base64 that said nothing
on a phone. Screenshots come back the same way, so the seat now re-encodes them
to something phone-sized and sends them as `shots` on the tool card, and the
guest app renders them inline with the card already open.

The pixels are kept on the last few only. The whole history is re-sent in the
snapshot on every reconnect, and a phone in a cafe reconnects a lot; older
cards go back to naming the image the way they did before there was anywhere
to show it.

## Run

### On a salon PC (`static`)

```bash
./scripts/salon-tls.sh
./scripts/salon-secrets.sh operator
./scripts/run-salon.sh    # :8080  (reads data/tls/host.token)
./scripts/run-seat.sh     # :8787 guests + :8788 mTLS control
./scripts/wifi-status.sh
```

Check-in **fails** if the seat control port does not accept the desk's client
cert. Set `BYOI_SEAT_CONTROL_URL` to the seat's current IP when the machines
are separate; you do not reissue certs when DHCP changes.

### On a cloud VM (`ondemand`)

One Linux VM with Docker. Point `<domain>` and `*.<domain>` at it first — the
wildcard is issued over DNS-01, so Caddy also needs an API token for the zone.

```bash
cp deploy/.env.example deploy/.env     # domain, ACME email, DNS provider token
./scripts/salon-secrets.sh operator    # the desk password
./scripts/salon-secrets.sh print-relay # token for the machine with the printer
./scripts/cloud-up.sh
```

That mints the salon CA if it is missing, builds the seat image, and brings up
Caddy and the desk. Nothing else is long-running: seats appear at check-in.

`BYOI_SEAT_CONTROL_URL` must be **unset** here. It pins every control call to one
address, which is right with one seat agent and wrong with one per session.

**The desk holds the Docker socket, and that is root on the VM.** It is the same
trust level it already had for sandboxed grading, but the blast radius is larger
now that it also raises seats. The desk never runs guest code, which is what
makes this tolerable; if you want it smaller, put a socket proxy in front,
restricted to what `apps/api/seats.py` and `apps/api/infra.py` actually call.

Seats are RAM on this VM rather than PCs somebody already owns, so
`BYOI_MAX_SEATS` caps how many can be up at once. Check-in past the cap is
refused with that in the message.

## The default board

A fresh `salon.db` opens with the fixes waiting on **The Fusion Studio** site
(`https://github.com/boscojacinto/thefusionstudio`), each with an acceptance
spec. They live in [`apps/api/seed_board.py`](../apps/api/seed_board.py) — edit
the briefs there, bump `SEED_VERSION`, and the next desk start publishes them.

Re-seeding never touches a brief the host wrote. A previous default is deleted
if no visit ever claimed it, and unpublished (so the visit still reads) if one
did.

The repo is cloned lazily: the folder appears the first time a guest claims one
of these briefs, or when the host taps **Fetch repo** in *New solution* to get
it out of the way before the doors open. Nothing on the desk touches the
network at startup.

## Projects on the solution board

Each brief can point at a **git project**. When the guest claims it, the seat
Claude session's working directory becomes that folder (not the empty
`data/workspace` sandbox).

On the desk (`http://127.0.0.1:8080/`):

1. **New project** — create a GitHub repo (`gh` must be logged in on this PC),
   clone an existing URL, or attach a local folder.
2. **Publish** a brief with that project selected, or assign a project on an
   existing brief.
3. Guest claims the brief → seat `cwd` switches to the project.

Where that folder is depends on the shape:

| | `static` (salon PC) | `ondemand` (cloud) |
|---|---|---|
| Seat `cwd` | `project.local_path` | `/app/data/workspace/<name>` |
| How it gets there | already the same disk | cloned at claim time |
| Board copy | opened directly | never mounted into a seat |

A seat container cannot simply open `project.local_path` — the projects folder
belongs to the desk. Mounting it in would be worse than an inconvenience: every
guest would get read and write over every other guest's work, the same hazard
that already keeps a visit to the Claude accounts it was allocated. So the desk
clones the project into that visit's own workspace
(`seats.seed_workspace`), which is a directory under
`data/seat-runtime/byoi-seat-<session>/` bind-mounted at `/app/data/workspace`.

Two things follow from it being a bind mount rather than a named volume: the
desk can read the guest's tree back, which is how grading fetches the submission
ref without the seat pushing anywhere (so a seat needs no git credentials), and
freeing the seat removes the workspace along with the rest of the visit.

The clone's `origin` is set to whatever the project calls `origin`, so a guest
running `git push` aims at the real remote and not at a path that exists only
inside the desk container. A project that is not a git repo is copied instead.

Each brief can include an **acceptance spec**: plain-English facts the
solution must satisfy, one per line. When the guest marks shipped, the phone
shows passing and failing cases — one per requirement in the spec.

The desk's **Specs & QA** tab is where the host writes and edits that spec —
at brief creation, or any time after, on an existing one. The next "I'm done"
against that brief is graded against whatever is saved there. The same tab
lists every graded visit, most recent first, with its pass/fail cases: the
seat and board panels stop tracking a visit the instant it completes (grading
still runs in the background against the seat's workspace), so this is the
only place on the desk to watch a suite run and see what it found.

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

### What the guest is shown

The phone gets every case with a ✓ or ✕ and, for a failure, one line on why —
failures first, since that is what you act on. It never gets the suite.

`/api/sessions/{id}/tests` is the only grading route a phone can reach without
operator auth, so it does not serve the stored report: that quotes the runner
verbatim, and a JUnit failure message carries the assertion source, the test
path and a traceback. `apps/api/guest_report.py` rebuilds it instead —

* the **label** is the host's own spec clause, or a test name with its path
  stripped when a case has no clause;
* the **reason** is written from templates in that module, filled only with
  tokens matched against an allowlist: a status code, a short scalar, an
  exception class name. Anything else degrades to "This check did not pass."
  rather than passing text through. The clause above it still names the
  behaviour that was missing, which is the actionable half;
* a **grader outage** (`grader_error`) is reported as "couldn't finish
  grading", not as a failed check — a dead pipeline must not read on the phone
  as the guest's bug.

The full report, raw details and all, stays on the operator-authed
`/api/sessions/grading` behind the Specs & QA tab.

The blind suite never reaches the seat at all — it is written and run on the
desk, against a fetched ref. The one path that does write into the guest's own
tree is the fallback seat verifier, which may need a test of its own; it is
steered to `.byoi-verify/` and that directory is deleted in a `finally`, so a
timeout or a crash cannot leave a test behind in a workspace the guest still
has a terminal on.

Log the host account in once, alongside the seat accounts:

```bash
./scripts/seat-claude-login.sh --account claude-host
CLAUDE_CONFIG_DIR=data/claude-accounts/claude-host claude auth login --claudeai
```

In the cloud the desk fetches the ref straight out of the seat's workspace: it
is a bind mount, so both containers see the same repository and nothing has to
be pushed anywhere. The seat needs no git credentials for grading to work.

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

### Where the stack runs

**`static`.** Claiming a brief whose project needs infrastructure starts a
per-session stack on the seat (`docker compose -p byoi-<session>`) with
**ephemeral host ports**, so several seats on one PC never collide.

**`ondemand`.** The **desk** starts it, and the seat never sees Docker at all.
This is not tidiness: the guest's Claude has Bash and inherits the seat's
environment, so a Docker socket on the seat is root on the VM — the same
reasoning that already keeps the Vercel token off it. The desk puts Postgres and
Redis on a network only that one seat is attached to, and hands the seat the
URLs over mTLS. They point at service names (`byoi-pg-<session>:5432`) rather
than a scraped host port, so two seats are kept apart by being on different
networks rather than by different port numbers.

Either way the seat owns the file: only the salon's own block of `.env.local` is
rewritten, so a guest's own variables survive. The stack and its volumes go when
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
./scripts/salon-secrets.sh operator     # the desk sign-in password
./scripts/salon-secrets.sh print-relay  # generates a token for the counter
./scripts/salon-secrets.sh vercel     # prompts, writes data/secrets/vercel.token 0600
./scripts/salon-secrets.sh neon
./scripts/salon-secrets.sh upstash
./scripts/salon-secrets.sh --list     # what is configured; never the values
```

| Credential | File | Effect if unset |
|---|---|---|
| operator password | `operator.hash` | Nobody can sign in to the desk |
| `BYOI_PRINT_RELAY_TOKEN` | `print-relay.token` | The counter's printer agent cannot claim slips |
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

1. Host checks the coder in.
   * `static` — the desk **POSTs the OTP to the seat over mTLS** and prints a
     slip. QR: `https://<seat-lan-ip>:8787/join?otp=<otp>`.
   * `ondemand` — the desk raises a seat container, mints its certificate,
     publishes `s-<session>.<domain>` on the edge, waits for it to answer on its
     control port, *then* pushes the OTP and queues the slip. The floor screen
     says **Raising the seat…** while that happens. A QR is only ever shown for
     an address that already answers.
2. `static` only: the phone joins the same Wi-Fi as the seat PC. A cloud seat is
   on the public internet — cellular is fine.
3. Open the QR (or **BYOI Guest** → **Scan slip QR**). The seat serves an
   installable PWA at `/guest/`.
4. Claim a brief, then **Open chat**. Seat checks OTP, issues a ticket for
   `/chat`, and the phone is a Claude Code session: messages, tool cards,
   diffs, todos, plan/code/auto/ask modes, slash commands (`/commit`,
   `/review`, `/model`, `/compact`, …), file mentions, photos, and stop.

Once the browser says the PWA is installable, the floor raises a sheet —
**Keep this seat on your phone** — over the session tab. It asks once a visit,
and only on the floor: not on the join screen, where the guest is typing a code,
and not in chat, where they are working. **Not now** dismisses it for the rest
of the visit, and the floor's ⋯ menu keeps an **Add to home screen** entry for
anyone who changes their mind. iOS gets the same sheet with Safari's
Share-menu sentence in place of the button, since there is no prompt to hold
there. The installed copy is the same app, and two things keep it that way
rather than a worse one:

* It launches on `start_url` with no `?otp=`, and a home-screen launch is a new
  browsing session — so the seat's OTP, unlock ticket and last screen live in
  `localStorage`, and tapping the icon mid-visit lands back in the chat. They
  are dropped on **Leave**, and when the seat says the slip is unknown (404) or
  the visit is over (410).
* Installed there is no browser chrome, so Android's back key is the only back
  there is. The app keeps one spare history entry and spends it on one step
  back — console, sheet, chat — re-arming until there is nothing left to close;
  only then does the press through and close the app.

Freeing the seat in `ondemand` destroys the container, its workspace volume, its
edge route, its Postgres and Redis, and its certificate — after revoking the
guest's own Claude token, so the refresh token does not outlive the tmpfs it sat
on. Every step is best-effort and recorded: an operator with a guest standing
there is never blocked by a container that will not die. A desk restart between
a check-in and a checkout is reconciled at startup — a seat with no live session
behind it is removed rather than left serving a guest who has gone home.

## Printing, when the desk is not in the room

The PeriPage speaks Bluetooth LE, which is a property of the room, so it stays at
the counter. In `ondemand` the desk composes the slip and queues it; a small
agent at the venue claims it and prints it through the same driver as always.

```bash
# on the machine with the printer, paired as usual
export BYOI_DESK_URL=https://salon.example.com
export BYOI_PRINT_RELAY_TOKEN=...        # from salon-secrets.sh print-relay
export PERIPAGE_MAC=C6:6C:09:0B:B2:50
./scripts/print-relay.py
```

It only makes outbound requests, so the counter can sit behind whatever NAT the
cafe has. The floor screen shows the printer as ok, offline, or with a queue
depth. **A check-in never waits on it**: the QR is on the desk screen either way,
so an empty roll or a closed laptop delays a piece of paper, not a visit. A claim
that is never finished is handed out again after two minutes — reprinting a slip
is cheap, never printing one is not.

## Backing up what cannot be recreated

```bash
./scripts/salon-backup.sh                 # -> data/backups/salon-<stamp>.tar.gz
./scripts/salon-backup.sh /mnt/elsewhere
```

Four paths, because losing them means redoing work no command can redo: the
`auth login` credentials in `data/claude-accounts/`, the salon CA in `data/tls/`
(re-minting it invalidates every seat identity), `data/secrets/`, and
`data/salon.db`. `salon.db` is copied through SQLite's backup API rather than
`cp`, since the desk is writing to it.

`data/projects/` is deliberately excluded — it is git, with an `origin` — as are
guest workspaces under `data/seat-runtime/`, which are destroyed at checkout.

On a cloud VM this sits *alongside* volume snapshots rather than replacing them:
a snapshot restores a dead machine, this restores one credential file without
rolling the whole disk back. The archive holds a private CA key and live Claude
credentials — treat it like an SSH key and copy it off the box.

## Optional SSH / TTY

SSH and `/tty` are operator side doors onto tmux. They are **not** the guest
path and SSH is **not** OTP-gated.

```bash
sudo ./scripts/seat-guest-ssh.sh
ssh guest@<seat-lan-ip>
```

`static` only. A cloud seat runs no sshd and is not published on port 22, and
an un-gated shell should not be reachable from the internet in the first place.
Use `docker exec -it byoi-seat-<session> bash` from the VM instead, which needs
an account on the VM — which the guest does not have.

## Environment

Everything new to running in the cloud, in one place. Defaults are the salon-PC
behaviour, so an existing checkout keeps working untouched.

| Env | Default | Meaning |
|---|---|---|
| `BYOI_SEATS` | `static` | `ondemand` makes the desk raise a seat container per visit |
| `BYOI_GUEST_NET` | `lan` | `public` drops the seat's private-address check (OTP still gates) |
| `BYOI_GUEST_TLS` | `1` | `0` when something in front terminates guest TLS |
| `BYOI_DOMAIN` | — | Seats are published at `s-<session>.<domain>` |
| `BYOI_PUBLIC_BASE` | — | Where the desk itself is reachable |
| `BYOI_CADDY_ADMIN` | — | Caddy's admin socket. Unset means no edge to publish on |
| `BYOI_SEAT_IMAGE` | `byoi-seat:latest` | Image a seat container is raised from |
| `BYOI_MAX_SEATS` | `4` | Concurrent seats; check-in past it is refused |
| `BYOI_SEAT_MEMORY` / `_CPUS` / `_PIDS` | `4g` / `2` / `1024` | Per-seat caps |
| `BYOI_SEAT_READY_TIMEOUT` | `120` | Seconds to wait for a new seat's control port |
| `BYOI_HOST_DATA_DIR` | — | Where `data/` lives on the VM, for bind mounts |
| `BYOI_EDGE_NETWORK` / `BYOI_CONTROL_NETWORK` | `byoi-edge` / `byoi-ctl` | Docker networks |
| `BYOI_PRINT_MODE` | `local` | `relay` queues slips for the venue's printer agent |
| `BYOI_OPERATOR_TTL` / `BYOI_OPERATOR_IDLE` | `43200` / `7200` | Desk session lifetime, in seconds |
| `BYOI_COOKIE_SECURE` | `1` | `0` only where the desk is served over plain HTTP |
| `BYOI_PREVIEW_PORT` | `3000` | Dev-server port published at `p-<session>.<domain>`. Empty turns previews off |
| `BYOI_SEAT_MCP` | `deploy/seat-mcp.json` | MCP servers the seat's Claude gets. Empty means none |

## What did not change

Worth stating, because a migration invites the assumption that it did:

* **The 80% early switch is still broken.** Nothing here touches it — see the
  note under *Claude login*. A real usage limit still switches accounts; the
  early, graceful one still never fires.
* **The QR geometry is untouched.** `render_qr` picks a whole number of pixels
  per module for a reason, and `scripts/qr-scan-report.py` still asserts it.
* **Grading is unchanged.** It always ran on the desk, in Docker, with
  `--network none` and a scrubbed environment. It now happens to sit next to the
  provisioning driver.
* **The URL contract is unchanged.** The app still reads `DATABASE_URL`,
  `REDIS_URL`, and `AUTH_SECRET`, and still branches on nothing. Only who starts
  the containers moved.
