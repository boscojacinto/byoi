# BYOI salon (coding + wellness)

Each **seat** is a Linux PC with its own Claude Code *terminal*. The guest
phone and that PC are on the **same Wi-Fi**. The TTY is HTTP + WebSocket —
no Bluetooth, no `192.168.44.1`, no Claude Remote Control.

```
Phone  ←── cafe Wi-Fi (HTTP / WS :8787) ──→  Seat tmux claude-guest
```

## Run

```bash
PYTHONPATH=src:. uvicorn apps.api.main:app --host 0.0.0.0 --port 8080
PYTHONPATH=src:. uvicorn apps.seat.main:app --host 0.0.0.0 --port 8787
./scripts/wifi-status.sh
```

The seat process listens on all interfaces. A phone on the LAN opens
`http://<seat-lan-ip>:8787/join?otp=…` (printed on the slip) and attaches
`tmux attach -t claude-guest` in the browser.

Set `BYOI_WIFI_SSID` to the cafe network name shown on the slip. Override
the QR host with `BYOI_JOIN_BASE` if the seat LAN address is not the one
`lan_ip()` finds. For a dedicated seat PC, set that row’s `agent_url` to
`http://<seat-lan-ip>:8787`.

## Floor

1. Host checks the coder in → slip (Wi-Fi name + QR to this seat).
2. Phone joins the same Wi-Fi as the seat PC (if it is not already on it).
3. Scan the QR, or open the URL in the guest app / browser.
4. You are on the seat TTY.

Expo Go and a mobile browser both work; they only need LAN HTTP to `:8787`.

## Optional SSH

Same TTY over SSH, still on cafe Wi-Fi:

```bash
sudo ./scripts/seat-guest-ssh.sh
ssh guest@<seat-lan-ip>
```
