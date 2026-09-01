import { useCallback, useEffect, useState } from "react";
import AsyncStorage from "@react-native-async-storage/async-storage";
import * as Linking from "expo-linking";
import Constants from "expo-constants";
import { seatStatus } from "./api";
import { guessSeatBase, normalizeBase, parseJoinUrl } from "./joinUrl";
import JoinScreen from "./screens/JoinScreen";
import ScanScreen from "./screens/ScanScreen";
import SeatScreen from "./screens/SeatScreen";

const LAST_SEAT = "byoi.seatBase";

// Three screens, and only one of them is the salon: find a seat, read its QR,
// then hand the visit to that seat's own guest UI in SeatScreen. Everything a
// guest does once seated lives in apps/guest-web, so it stays the same UI
// whether they came in through this app or through the browser on the slip.
export default function App() {
  const [screen, setScreen] = useState("join");
  const [host, setHost] = useState("");
  const [otp, setOtp] = useState("");
  const [base, setBase] = useState("");
  const [status, setStatus] = useState("Same Wi-Fi as the seat PC. Scan the slip QR.");
  const [busy, setBusy] = useState(false);

  const applyUrl = useCallback((raw) => {
    const parsed = parseJoinUrl(raw);
    if (!parsed) return null;
    if (parsed.base) setHost(parsed.base + (parsed.otp ? `/join?otp=${parsed.otp}` : ""));
    if (parsed.otp) setOtp(parsed.otp);
    return parsed;
  }, []);

  const sit = useCallback(async (rawHost, rawOtp) => {
    const parsed = parseJoinUrl(rawHost) || { base: normalizeBase(rawHost), otp: rawOtp };
    const nextBase = parsed.base || normalizeBase(rawHost);
    const nextOtp = parsed.otp || (rawOtp || "").trim();
    if (!nextBase) {
      setStatus("Scan the slip QR, or paste the join URL from the host desk.");
      return;
    }
    setBusy(true);
    setStatus("reaching the seat…");
    try {
      // Ask the seat before opening it, so a phone on the wrong Wi-Fi gets
      // told that here rather than as a blank page inside the WebView.
      await seatStatus(nextBase);
      await AsyncStorage.setItem(LAST_SEAT, nextBase);
      setBase(nextBase);
      setOtp(nextOtp);
      setScreen("seat");
      setStatus("");
    } catch (err) {
      setStatus(err.message || "Cannot reach the seat. Same Wi-Fi? Seat agent on :8787?");
      setScreen("join");
    } finally {
      setBusy(false);
    }
  }, []);

  useEffect(() => {
    (async () => {
      const saved = await AsyncStorage.getItem(LAST_SEAT);
      setHost(saved || guessSeatBase(Constants));
    })();
  }, []);

  useEffect(() => {
    function onUrl({ url }) {
      const parsed = applyUrl(url);
      if (parsed?.base) sit(parsed.base, parsed.otp);
    }
    Linking.getInitialURL().then((url) => {
      if (url && !url.startsWith("exp://")) onUrl({ url });
    });
    const sub = Linking.addEventListener("url", onUrl);
    return () => sub.remove();
  }, [applyUrl, sit]);

  if (screen === "scan") {
    return (
      <ScanScreen
        onCancel={() => setScreen("join")}
        onCode={(data) => {
          const parsed = applyUrl(data);
          if (!parsed?.base) {
            setStatus("That QR is not a BYOI join link.");
            setScreen("join");
            return;
          }
          setScreen("join");
          sit(parsed.base, parsed.otp);
        }}
      />
    );
  }

  if (screen === "seat" && base) {
    return (
      <SeatScreen
        base={base}
        otp={otp}
        onLeave={() => {
          setScreen("join");
          setStatus("Left the seat. Scan again to sit.");
        }}
      />
    );
  }

  return (
    <JoinScreen
      host={host}
      otp={otp}
      status={status}
      busy={busy}
      onChangeHost={(text) => {
        setHost(text);
        const parsed = parseJoinUrl(text);
        if (parsed?.otp) setOtp(parsed.otp);
      }}
      onChangeOtp={setOtp}
      onScan={() => setScreen("scan")}
      onSit={() => sit(host, otp)}
    />
  );
}
