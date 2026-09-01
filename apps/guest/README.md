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

## What is native and what is not

The app is a shell. Everything a guest does once seated — the floor, the
Solutions tab, claiming a brief, the chat with its tool cards and diffs and
permission prompts, the preview link, deploys, "I'm done" and the spec results
— is [`apps/guest-web`](../guest-web), served by the seat and shown here in one
full-screen WebView (`screens/SeatScreen.js`). There is no second copy of any
of it, so the app cannot fall behind the page.

Native is only what a page on a phone cannot do for itself:

| | |
|---|---|
| `plugins/withByoiCa.js` | Trusts the salon CA, which is the whole reason to install an app |
| `screens/ScanScreen.js` | Reads the slip QR with the camera; the PWA can only take a typed code |
| `screens/JoinScreen.js` | Finds a seat before there is a page to show |
| `App.js` | `byoi://` and `https://…/join?otp=` deep links |
| `screens/SeatScreen.js` | The Android back key, off-seat links, and saving a file |

Three of those need the page and the shell to agree, so the page offers them
and works the same without a shell (`apps/guest-web/guest.js`):

* **Back.** `window.byoiBack()` takes one step back — close the console, close
  a sheet, leave chat for the floor — and returns false when there is nothing
  left to close. Only then does the hardware key mean *leave the seat*.
* **Leaving.** The floor's "Leave" hands back to the app's join screen, where
  the scanner is, rather than to the PWA's own code box.
* **Files.** `/export` and `/handoff` download in a browser. An Android WebView
  never sees a `blob:` URL as a download, so under the shell the same text goes
  to the phone's share sheet instead.

Off-seat links — the dev server on `:3000`, a deployed preview, Claude's own
sign-in page for *use my own Claude account* — open in the phone's browser. A
guest who opened Claude sign-in inside the WebView would have no way back.

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
4. Claim a brief → **Chat**.
