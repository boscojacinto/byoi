---
name: deploy-utho
description: Deploy the BYOI salon to a Utho cloud VM — provision the instance, point DNS, install Docker, carry the salon's state across, and run scripts/cloud-up.sh. Use when asked to deploy to the cloud or to Utho, to stand up / move / rebuild the salon VM, to check on it, or to tear it down.
---

# Deploying the salon to Utho

This puts the `ondemand` salon on one Utho VM: Caddy on 443 with a wildcard
certificate, the desk behind it, and a seat container raised per check-in at
`https://s-<session>.<domain>`. It is the cloud half of [`docs/salon.md`](../../../docs/salon.md);
`scripts/cloud-up.sh` already does the last step, and this skill is everything
that has to be true before that script can run.

Two files live next to this one:

| | |
|---|---|
| `utho.py` | The Utho v2 API — plans, zones, SSH keys, create, wait, ip, destroy |
| `vm-bootstrap.sh` | Run once on the fresh VM: Docker, ufw, openssl, python3 |

## Before touching the API

Creating an instance commits the user's account to a month of billing up front,
and destroying one deletes a disk without refunding the rest of that month.
**Confirm the zone, the plan, and the monthly cost with the user before
`utho.py create`, and never run `utho.py destroy` without being asked to.** Show
them the `plans` row you intend to use.

Check these first, and stop and ask if any are missing rather than inventing
values:

* An Utho Personal Access Token, at `~/.config/byoi/utho.token` (0600) or
  `$UTHO_API_TOKEN`. Generate at <https://console.utho.com/switch/api>.
  Do **not** put it in `data/secrets/` — that directory is copied to the VM and
  goes into `salon-backup` archives, and this token can destroy every server on
  the account. The VM never needs it.
* A domain, with its DNS at a provider Caddy has a DNS-01 module for
  (`BYOI_DNS_PLUGIN`, Cloudflare by default), and an API token for that zone.
  The wildcard is issued over DNS-01, so this is not optional.
* An SSH keypair. `utho.py key-import` uploads the public half.

Verify the token before doing anything else:

```bash
cd .claude/skills/deploy-utho
./utho.py check
```

## 1. Local preflight

Everything here happens in the repo, before a VM exists.

```bash
cp deploy/.env.example deploy/.env    # then edit — see below
./scripts/salon-secrets.sh operator   # the desk password; cloud-up.sh refuses without it
./scripts/salon-secrets.sh print-relay # only if the PeriPage stays at the counter
```

In `deploy/.env`: `BYOI_DOMAIN`, `BYOI_ACME_EMAIL`, `BYOI_DNS_PROVIDER`,
`BYOI_DNS_PLUGIN`, `BYOI_DNS_API_TOKEN`, and `BYOI_MAX_SEATS` sized to the plan
you are about to buy. `BYOI_SEAT_CONTROL_URL` must be **unset** — it pins every
control call to one address, which is wrong when there is a seat per session.

If this salon has run before and you want to keep the CA, the operator password,
the Claude accounts, and the board, take an archive now — it is how state moves
to the VM in step 5:

```bash
./scripts/salon-backup.sh
```

## 2. Pick a zone and a plan

```bash
./utho.py zones
./utho.py plans --min-ram 16
./utho.py images ubuntu
```

Sizing: each seat is a container capped at `BYOI_SEAT_MEMORY` (4g) and
`BYOI_SEAT_CPUS` (2), and the desk, Caddy, and the per-project Postgres and
Redis sit alongside. Budget **4g per concurrent seat plus 2g**, so
`BYOI_MAX_SEATS=4` wants a 16–18g plan. Disk: 80g is comfortable — the seat
image, the templates, and the graded runs are all Docker layers.

Pick the zone nearest the venue; the guest's phone is on cellular and every
keystroke crosses it.

## 3. Create the VM

Name it **the salon domain**. `utho.py` looks servers up by hostname, and it is
the name the operator will remember six months from now.

```bash
./utho.py key-import salon ~/.ssh/id_ed25519.pub    # once per account

export UTHO_DCSLUG=innoida          # from ./utho.py zones
export UTHO_PLANID=10045            # from ./utho.py plans
export UTHO_SSHKEYS=<key id>        # from ./utho.py key-import
export UTHO_IMAGE=ubuntu-24.04-x86_64
./utho.py create salon.example.com

IP="$(./utho.py wait salon.example.com)"   # blocks until sshd answers
echo "$IP"
```

Without `UTHO_SSHKEYS`, set `UTHO_ROOT_PASSWORD` instead — `create` refuses to
deploy a box nobody can log into. A root password Utho generates is printed once
and never again.

`create` deploys on the **monthly** cycle and prints which cycle it used. That is
the right default for a standing venue, and it means the plan you picked in step
2 is a monthly commitment, not a meter you can stop on Sunday. Set
`UTHO_BILLINGCYCLE=hourly` only for a box you will genuinely destroy within days.

## 4. Point DNS at it

Both records, at the zone's own provider — not at Utho, since Caddy solves
DNS-01 through `BYOI_DNS_PROVIDER`:

| Record | Type | Value |
|---|---|---|
| `<domain>` | A | the VM's IPv4 |
| `*.<domain>` | A | the VM's IPv4 |

On Cloudflare, set both to **DNS only** (grey cloud). Proxied records terminate
TLS at Cloudflare, and the seat's own front door expects to reach Caddy.

Do this before step 6: the first request waits on the DNS-01 challenge, and a
wildcard takes a minute to propagate.

## 5. Bootstrap the VM and put the checkout on it

```bash
scp .claude/skills/deploy-utho/vm-bootstrap.sh root@"$IP":/tmp/
ssh root@"$IP" bash /tmp/vm-bootstrap.sh
```

It prints how many seats the box's RAM actually supports — reconcile that with
`BYOI_MAX_SEATS` before continuing.

Then the code. `data/` is gitignored, so a clone alone leaves the VM without a
CA, an operator password, or any Claude account:

```bash
rsync -av --delete \
  --exclude .git --exclude .venv --exclude '**/node_modules' \
  --exclude '**/__pycache__' --exclude .pytest_cache --exclude data \
  ./ root@"$IP":/opt/byoi/
scp deploy/.env root@"$IP":/opt/byoi/deploy/.env
```

State moves one of two ways:

* **Carrying a salon across** — copy the archive from step 1 and unpack it:
  ```bash
  scp data/backups/salon-<stamp>.tar.gz root@"$IP":/tmp/
  ssh root@"$IP" 'mkdir -p /opt/byoi/data && tar -xzf /tmp/salon-<stamp>.tar.gz -C /opt/byoi/data && rm /tmp/salon-<stamp>.tar.gz'
  ```
* **Starting fresh** — skip it. `cloud-up.sh` mints the CA itself, but the
  operator password is not in the repo, so set it on the VM:
  ```bash
  ssh root@"$IP" 'cd /opt/byoi && ./scripts/salon-secrets.sh operator'
  ```

Either way, confirm before the next step:

```bash
ssh root@"$IP" 'cd /opt/byoi && ./scripts/salon-secrets.sh --list'
```

## 6. Bring it up

```bash
ssh root@"$IP" 'cd /opt/byoi && ./scripts/cloud-up.sh'
```

That builds the seat image, brings up Caddy and the desk, and prints the URLs.
Nothing else is long-running — seats appear at check-in.

## 7. Verify

```bash
ssh root@"$IP" 'cd /opt/byoi && docker compose --env-file deploy/.env -f deploy/docker-compose.yml ps'
curl -sS -o /dev/null -w '%{http_code} %{ssl_verify_result}\n' https://salon.example.com/
```

Expect `200 0` — a real certificate, verified. If the certificate is missing,
the DNS-01 challenge is the thing to look at:

```bash
ssh root@"$IP" 'cd /opt/byoi && docker compose --env-file deploy/.env -f deploy/docker-compose.yml logs caddy | tail -50'
```

Then sign in at `https://<domain>/` with the operator password and check a guest
in. A session should answer at `https://s-<session>.<domain>/guest/` on the same
wildcard certificate.

## Day two

```bash
./utho.py list                       # what exists, and whether it is running
./utho.py show salon.example.com     # monthly cost, next due date, RAM
ssh root@"$IP" 'cd /opt/byoi && ./scripts/salon-backup.sh'
```

Redeploying code is the same `rsync` from step 5 followed by `cloud-up.sh` —
it is idempotent and rebuilds only what changed. Seats that are up at the time
are containers the desk owns; free them from the desk first.

**The rsync deletes `deploy/.env` every time.** It is gitignored, so it does
not exist in the local source tree, and the command uses `--delete`. Re-run
the `scp deploy/.env root@"$IP":/opt/byoi/deploy/.env` from step 5 immediately
after every redeploy rsync, day two included — not just the first time.

## Teardown

Only when the user asks. Back up first — the archive holds the salon CA key, the
operator hash, and live Claude credentials, none of which can be recreated:

```bash
ssh root@"$IP" 'cd /opt/byoi && ./scripts/salon-backup.sh'
scp root@"$IP":/opt/byoi/data/backups/salon-*.tar.gz ./
./utho.py destroy salon.example.com    # prompts for the hostname
```

Then remove the two DNS records, or the domain keeps pointing at an IP Utho has
handed to somebody else.

## Things that bite

* **The desk holds the Docker socket**, which is root on this VM. That is the
  same trust it already had for sandboxed grading, but the blast radius is
  bigger now that it also raises seats. The desk never runs guest code, which is
  what makes it tolerable.
* **`BYOI_SEAT_CONTROL_URL` must stay unset in the cloud.** Set, it pins every
  control call to one address and check-in fails from the second seat on.
* **`deploy/.env` and `.env` are gitignored and excluded from the Docker build
  context.** They have to be copied to the VM by hand; nothing will do it for
  you, and the failure is a container that refuses to start on a missing
  variable.
* **The rsync in step 5 runs `--delete` against `/opt/byoi/`.** If your shell's
  cwd has drifted — e.g. an earlier `cd .claude/skills/deploy-utho` for
  `utho.py` left it there — `rsync ./ ... --delete` syncs *that* directory as
  if it were the whole repo and deletes everything else on the VM that isn't
  in it. Run `pwd` (or use an absolute source path) immediately before this
  rsync, every time, especially right after any `cd` earlier in the same
  session. If it happens anyway: don't panic, the running containers are
  untouched until you restart them — `docker inspect <container> --format
  '{{range .Config.Env}}{{println .}}{{end}}'` recovers `deploy/.env` and
  root `.env` values straight from the live process before you rebuild
  anything.
* **Utho answers errors with HTTP 200** and a `status` field. `utho.py` checks
  the body; anything else talking to this API should too.
* **The instance bills monthly, and the month is bought up front.** Powering it
  off saves nothing, destroying it mid-cycle refunds nothing, and a rebuild in
  the same month is a second month. Treat the VM as standing infrastructure and
  redeploy onto it (step 5 again) rather than recreating it. `UTHO_BILLINGCYCLE`
  overrides this per deploy — Utho takes `hourly`, `monthly`, `3month`,
  `6month`, `12month` — and hourly is the honest choice only for a box you
  genuinely intend to destroy within days.
