# BYOI Guest

Android-first phone app for the vibe coder. The phone and the seat PC are
on the **same Wi-Fi**. It does **not** use Bluetooth or Claude Remote Control.

```
Phone  ←── cafe Wi-Fi (HTTP / WebSocket) ──→  Seat PC :8787
```

Scan the slip QR with the camera (opens the coder PWA in the browser), or
enter the seat address here and open the board / TTY in a WebView.

## Run

Seat agent must already be up (`--host 0.0.0.0 --port 8787`).

SDK 54 needs **Node ≥ 20**. This laptop’s `/usr/bin/node` is 18; Hermes Node 22 is already in `~/.local/bin`.

```bash
cd apps/guest
export PATH="$HOME/.local/bin:$PATH"   # Node 22
node -v                                  # should print v22.x
npx expo start
```

Expo Go must be SDK 54. Open it on the **same Wi-Fi** as the seat. On the
floor, build an APK:

```bash
npx expo run:android
# or EAS: npx eas build -p android --profile preview
```

Android needs cleartext HTTP to the seat LAN address (already set in `app.json`).

## Floor

1. Host checks you in, prints the slip (Wi-Fi name + QR).
2. Phone: join that Wi-Fi if you are not already on it.
3. Scan the QR, or open **BYOI Guest**, type the seat URL, **Find seat**.
4. **Open seat** (board) or **Terminal only** (same `tmux attach -t claude-guest`).

SSH is the same TTY: `ssh guest@<seat-lan-ip>`.
