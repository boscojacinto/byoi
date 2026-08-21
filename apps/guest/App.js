import { useCallback, useEffect, useState } from "react";
import AsyncStorage from "@react-native-async-storage/async-storage";
import * as Linking from "expo-linking";
import {
  claimBrief,
  completeSession,
  fetchBoard,
  joinSlip,
  seatStatus,
  unlockSeat,
} from "./api";
import Constants from "expo-constants";
import { guessSeatBase, normalizeBase, parseJoinUrl } from "./joinUrl";
import FloorScreen from "./screens/FloorScreen";
import JoinScreen from "./screens/JoinScreen";
import ScanScreen from "./screens/ScanScreen";
import TermScreen from "./screens/TermScreen";

const LAST_SEAT = "byoi.seatBase";

export default function App() {
  const [screen, setScreen] = useState("join");
  const [host, setHost] = useState("");
  const [otp, setOtp] = useState("");
  const [base, setBase] = useState("");
  const [join, setJoin] = useState(null);
  const [status, setStatus] = useState("Same Wi-Fi as the seat PC. Scan the slip QR.");
  const [busy, setBusy] = useState(false);
  const [ticket, setTicket] = useState("");

  const applyUrl = useCallback((raw) => {
    const parsed = parseJoinUrl(raw);
    if (!parsed) return false;
    if (parsed.base) setHost(parsed.base + (parsed.otp ? `/join?otp=${parsed.otp}` : ""));
    if (parsed.otp) setOtp(parsed.otp);
    return parsed;
  }, []);

  const sit = useCallback(
    async (rawHost, rawOtp) => {
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
        await seatStatus(nextBase);
        await AsyncStorage.setItem(LAST_SEAT, nextBase);
        setBase(nextBase);
        setOtp(nextOtp);
        if (nextOtp) {
          const data = await joinSlip(nextBase, nextOtp);
          setJoin({
            ...data,
            board: data.board || [],
            item: data.item || null,
          });
          setStatus(`${data.seat?.name || "Seat"} · hello ${data.session?.coder_name || ""}`);
        } else {
          const board = await fetchBoard(nextBase);
          setJoin({ session: null, seat: null, board, wifi_ssid: null });
          setStatus("Seat is up. Scan the slip QR to unlock your session.");
        }
        setScreen("floor");
      } catch (err) {
        setStatus(err.message || "Cannot reach the seat. Same Wi-Fi? Seat agent on :8787?");
        setScreen("join");
      } finally {
        setBusy(false);
      }
    },
    []
  );

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

  async function onClaim(boardId) {
    if (!join?.session) return;
    setBusy(true);
    try {
      const data = await claimBrief(base, join.session.id, boardId);
      setJoin((prev) => ({
        ...prev,
        session: data.session || prev.session,
        item: data.item || prev.item,
      }));
      setStatus("brief claimed · attach the TTY");
    } catch (err) {
      setStatus(err.message);
    } finally {
      setBusy(false);
    }
  }

  async function onAttach() {
    setBusy(true);
    try {
      const data = await unlockSeat(base, otp, join?.session?.id);
      setTicket(data.ticket || "");
      setScreen("term");
    } catch (err) {
      setStatus(err.message);
    } finally {
      setBusy(false);
    }
  }

  async function onShipped() {
    if (!join?.session) return;
    setBusy(true);
    try {
      await completeSession(base, join.session.id);
      setStatus("shipped. leave the seat.");
      setJoin((prev) => ({ ...prev, session: { ...prev.session, status: "done" } }));
    } catch (err) {
      setStatus(err.message);
    } finally {
      setBusy(false);
    }
  }

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

  if (screen === "term" && base) {
    return <TermScreen base={base} ticket={ticket} onClose={() => setScreen("floor")} />;
  }

  if (screen === "floor") {
    return (
      <FloorScreen
        join={join}
        status={status}
        busy={busy}
        onClaim={onClaim}
        onAttach={onAttach}
        onShipped={onShipped}
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
