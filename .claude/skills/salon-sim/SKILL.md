---
name: salon-sim
description: Run the salon against scripts/fake-claude.py instead of Claude Code — an isolated desk and seat on 127.0.0.1 for exercising check-in, chat, submission, and quota failover without spending a real account or finding a phone. Use when developing or debugging salon flows, reproducing a floor bug locally, or running the browser sim.
---

# The salon against a fake Claude

For working on the salon rather than running one. A real visit costs a Claude
account, a phone, and a person; this costs a terminal. The switch is one
environment variable — `BYOI_CLAUDE` — which every call site already reads
(`apps/seat/claude_chat.py:36`, `apps/api/testgen.py:23`, `apps/seat/verify.py`,
`apps/seat/tmux_claude.py`).

To run a *real* salon on this PC, use [`salon-local`](../salon-local/SKILL.md).

Next to this file:

| | |
|---|---|
| `sim-up.sh` | An isolated desk + seat on 18080/18787/18788, wired to the fake |

## Read this before trusting a green result

`scripts/fake-claude.py` writes `last-usage.json` itself. Real Claude Code, run
the way the seat runs it, **does not** — `statusLine` is never invoked under
`-p --output-format stream-json`. So the 80% compact-then-switch path passes
against the fake and has never once fired in production.

That is not a hypothetical: it is how the bug got missed, and it is written up
at [`docs/salon.md:155`](../../../docs/salon.md). Treat any quota-failover result
from this harness as evidence about the harness. The **hard** limit path is
different and does work — `parse_limit_error` reads the error off the stream.

More generally: the fake models the stream-json protocol, not Claude. Tool-use
shapes, refusals, token accounting, and latency are all wrong here by
construction.

## Tests first

Most salon behaviour is covered without standing anything up, and the suite
already points `BYOI_CLAUDE` at the fake where it needs to:

```bash
source .venv/bin/activate
pytest                          # tests/, quiet, src+. on the path
pytest tests/test_claude_chat.py -x
```

If a change is meant to alter floor behaviour and no test moved, that is worth a
second look before reaching for the browser.

## An isolated salon

```bash
./scripts/salon-tls.sh          # once — the sim reads data/tls, never rewrites it
.claude/skills/salon-sim/sim-up.sh
```

It prints the three URLs and the operator password (`sim` by default), then
blocks; Ctrl-C stops both processes.

| | |
|---|---|
| Desk | <http://127.0.0.1:18080/> |
| Guest | <http://127.0.0.1:18787/guest/> |
| Logs | `data/sim/logs/{desk,seat}.log` |

What "isolated" means, concretely: its own `BYOI_DATA`, `BYOI_SECRETS_DIR`,
account pool, workspace, and handoffs directory, all under `data/sim/`. It never
touches `data/salon.db`, `data/secrets/`, or `data/claude-accounts/`. It binds to
127.0.0.1 only — the operator password is printed on your terminal, so it has no
business being reachable from the LAN. Ports are 18080/18787/18788 so a real
salon on 8080/8787 can keep running beside it.

The tree is wiped and rebuilt on each start, because a `salon.db` from an older
schema fails in ways that look convincingly like real bugs. `BYOI_SIM_KEEP=1`
keeps it when you are mid-investigation.

Guest TLS is off (`BYOI_GUEST_TLS=0`) since the guest here is a browser on this
machine and there is no phone to install a CA onto. The **control** port keeps
its mTLS — that is what check-in rides on, so turning it off would stop testing
the thing most likely to break.

## The browser sim

`scripts/sim-failover-browser.py` is the end-to-end version: it stands up its own
salon under `data/sim-failover/`, drives Chrome over CDP as both the desk and the
guest, and screenshots each step into `data/sim-failover/screenshots/`.

```bash
./scripts/sim-failover-browser.py
```

It needs Chrome and a `DISPLAY`. Use it for flows that only exist in the browser
— the floor screen, the PWA, handoff downloads. Use `sim-up.sh` when you want to
poke at the thing by hand instead of watching a script do it.

Note it drives the *failover* story specifically, so the caveat at the top of
this file applies to it in full.

## What is worth exercising here

* **Check-in.** The desk POSTs the OTP to the seat over mTLS. This is where a
  broken certificate shows up, and `sim-up.sh` rewrites `seats.agent_url` in the
  fresh database because the desk otherwise looks for the seat on :8787.
* **The OTP gate and its lockout** — `apps/seat/gate.py`, eight failures.
* **Submission.** "I'm done" pins the guest tree to
  `refs/byoi/submissions/<session>` through a scratch index. The guest's branches
  are never touched, which is easy to break and easy to verify here.
* **Grading.** Needs Docker and runs `--network none`; the suite is generated
  with `--allowedTools ""` so it cannot read the code it judges. Against the fake
  the *shape* is testable, the judgement is not.

## Things that bite

* **A green failover test means nothing.** See the top of this file.
* **`data/sim/` is disposable and `data/` is not.** The sim writes only under its
  own directory, but a hand-rolled invocation that forgets `BYOI_DATA` and
  `BYOI_SECRETS_DIR` will happily overwrite the real operator password. Prefer
  `sim-up.sh` over reconstructing the environment from memory.
* **`BYOI_CLAUDE` must be an absolute path.** It is resolved with
  `shutil.which` and then executed directly.
* **`.credentials.json` containing `{}` is how the sim fakes an account.** That
  is also exactly what a botched `claude setup-token` leaves behind on a real
  seat, so do not copy the pattern into `salon-local`'s pool.
* **Ctrl-C stops the processes, not Docker.** A brief that raised Postgres and
  Redis leaves them running; `docker ps` after a session that got that far.
