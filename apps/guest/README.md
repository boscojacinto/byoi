# BYOI Guest

Android-first **app** for the vibe coder. The phone and the seat PC are on
the **same Wi-Fi**. This is the guest interface: scan the slip, claim a
brief, attach the seat TTY over **HTTPS**.

```
Phone app  ←── cafe Wi-Fi (HTTPS / WSS :8787) ──→  Seat PC
```

The salon CA (`scripts/salon-tls.sh`) is copied to `assets/ca.pem`. A
dev/APK build (`npx expo run:android`) trusts it. **Expo Go does not.**

## Floor APK (HTTPS)

```bash
# from the repo root, after salon-tls.sh
cd apps/guest
export PATH="$HOME/.local/bin:$PATH"
npx expo run:android
```

## Expo Go (dev only, will fail TLS)

Expo Go cannot install the salon CA. Use the APK on the floor.

## Floor

1. Host checks you in, prints the slip (Wi-Fi name + HTTPS QR).
2. Phone: join that Wi-Fi if you are not already on it.
3. Open **BYOI Guest** → **Scan slip QR** (`https://…/join?otp=`).
4. Claim a brief → **Attach TTY**.
