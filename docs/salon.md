# BYOI salon (coding + wellness)

Each **seat** is a Linux PC with its own Claude Code *terminal*. The guest
phone and that PC are on the **same Wi-Fi**. Guests never log into Claude.
The seat operator runs `claude setup-token` once; guests attach
`tmux claude-guest` only after an **OTP** the host printed on the slip.

```
Host desk  --mTLS + token-->  Seat control :8788  (admit / revoke OTP)
Phone app  --HTTPS + OTP--->  Seat guest   :8787  --ticket-->  tmux
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

Then export `BYOI_TLS_DIR`, `BYOI_HOST_TOKEN_FILE`, and
`BYOI_SEAT_CONTROL_URL=https://<seat-ip>:8788` (loopback if both processes
are on one PC). Paste the token into the host page:

```js
localStorage.setItem("byoiHostToken", "<contents of data/tls/host.token>")
```

Keep `ca-key.pem` off the cafe LAN.

Guests use **HTTPS** on `:8787` with the same salon CA. The seat certificate
lists the current LAN IPs in SAN so `https://<seat-ip>:8787` verifies. If
DHCP moves the seat, re-run `./scripts/salon-tls.sh` (CA stays; seat cert is
reissued). The guest APK trusts `apps/guest/assets/ca.pem` (copied by that
script). **Expo Go cannot trust a private CA** — use
`cd apps/guest && npx expo run:android`.

## Claude login (seat PC, once)

```bash
./scripts/seat-claude-login.sh
claude setup-token
```

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

## Floor

1. Host checks the coder in. Desk **POSTs the OTP to the seat over mTLS**,
   then prints a slip. QR: `https://<seat-lan-ip>:8787/join?otp=<otp>`.
2. Phone joins the same Wi-Fi as the seat PC.
3. Open **BYOI Guest**, tap **Scan slip QR**.
4. **Attach TTY** — seat checks OTP, then issues a ticket for `/term`.

```bash
cd apps/guest
export PATH="$HOME/.local/bin:$PATH"
npx expo start
```

## Optional SSH

SSH is a side door onto the same tmux; it is **not** OTP-gated.

```bash
sudo ./scripts/seat-guest-ssh.sh
ssh guest@<seat-lan-ip>
```
