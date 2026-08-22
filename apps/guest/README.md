# BYOI Guest

Android-first **app** wrapping the same guest PWA the slip QR opens. The
phone and the seat PC are on the **same Wi-Fi**. Scan the slip, claim a
brief, chat with Claude Code over **HTTPS** — not a TTY.

```
Phone  ←── cafe Wi-Fi (HTTPS / WSS :8787) ──→  Seat PC  ──→  Claude Code
```

Prefer the **PWA** at `https://<seat>:8787/guest/` (Add to Home Screen). The
salon CA (`scripts/salon-tls.sh`) is copied to `assets/ca.pem`. A dev/APK
build (`npx expo run:android`) trusts it. **Expo Go does not.**

## Floor APK (HTTPS)

```bash
# from the repo root, after salon-tls.sh
cd apps/guest
export PATH="$HOME/.local/bin:$PATH"
npx expo run:android
```

## Floor

1. Host checks you in, prints the slip (Wi-Fi name + HTTPS QR).
2. Phone: join that Wi-Fi if you are not already on it.
3. Open the QR, or **BYOI Guest** → **Scan slip QR** (`https://…/join?otp=`).
4. Claim a brief → **Open chat**.
