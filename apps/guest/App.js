import { useMemo, useState } from "react";
import {
  Pressable,
  SafeAreaView,
  ScrollView,
  StatusBar,
  StyleSheet,
  Text,
  TextInput,
  View,
} from "react-native";
import { WebView } from "react-native-webview";

const PAPER = "#f4efe4";
const INK = "#2b2118";
const ESPRESSO = "#5c3317";
const SAGE = "#6b7f6a";

function normalizeBase(raw) {
  let s = (raw || "").trim();
  if (!s) return "";
  if (!/^https?:\/\//i.test(s)) s = "http://" + s;
  return s.replace(/\/$/, "");
}

export default function App() {
  const [host, setHost] = useState("");
  const [otp, setOtp] = useState("");
  const [page, setPage] = useState(null);
  const [status, setStatus] = useState("Join the same Wi-Fi as the seat PC, then enter the address from the slip.");
  const [seat, setSeat] = useState(null);

  const base = useMemo(() => normalizeBase(host), [host]);

  async function probe() {
    if (!base) {
      setStatus("Enter the seat address from the slip (http://192.168.x.x:8787).");
      return;
    }
    try {
      const res = await fetch(`${base}/local/status`);
      const data = await res.json();
      setSeat(data);
      setStatus(`${data.name || "Seat"} up · ${data.tmux || "tmux"}`);
    } catch {
      setSeat(null);
      setStatus("Cannot reach the seat. Same Wi-Fi? Is the seat agent on :8787?");
    }
  }

  if (page && base) {
    const path =
      page === "tty" ? "/tty" : `/coder${otp ? `?otp=${encodeURIComponent(otp)}` : ""}`;
    return (
      <View style={{ flex: 1, backgroundColor: PAPER }}>
        <StatusBar barStyle="dark-content" backgroundColor={PAPER} />
        <SafeAreaView style={styles.safe}>
          <Pressable style={styles.btnGhost} onPress={() => setPage(null)}>
            <Text style={styles.btnGhostText}>Close seat</Text>
          </Pressable>
          <WebView
            source={{ uri: `${base}${path}` }}
            originWhitelist={["*"]}
            style={{ flex: 1, backgroundColor: PAPER }}
          />
        </SafeAreaView>
      </View>
    );
  }

  return (
    <View style={{ flex: 1, backgroundColor: PAPER }}>
      <StatusBar barStyle="dark-content" backgroundColor={PAPER} />
      <SafeAreaView style={styles.safe}>
        <ScrollView contentContainerStyle={{ paddingBottom: 40 }} keyboardShouldPersistTaps="handled">
          <Text style={styles.eyebrow}>BYOI guest</Text>
          <Text style={styles.title}>Sit. Same Wi-Fi. Attach.</Text>
          <Text style={styles.lede}>
            This phone and the seat PC are on the same Wi-Fi. Scan the slip QR in
            the camera app, or type the seat address here. No Bluetooth, no
            Claude Remote Control.
          </Text>

          <View style={styles.card}>
            <Text style={styles.step}>1. Join the cafe Wi-Fi printed on the slip.</Text>
            <Text style={styles.step}>2. Scan the QR, or enter the seat host below.</Text>
            <Text style={styles.step}>3. Open the board or the TTY — that is tmux claude-guest.</Text>
          </View>

          <Text style={styles.label}>Seat address</Text>
          <TextInput
            style={styles.input}
            value={host}
            onChangeText={setHost}
            placeholder="http://192.168.x.x:8787"
            placeholderTextColor="#9a8b7a"
            autoCapitalize="none"
            autoCorrect={false}
            keyboardType="url"
          />
          <Text style={styles.label}>OTP (optional)</Text>
          <TextInput
            style={styles.input}
            value={otp}
            onChangeText={setOtp}
            placeholder="from the slip"
            placeholderTextColor="#9a8b7a"
            autoCapitalize="none"
            autoCorrect={false}
          />

          <Text style={styles.status}>{status}</Text>
          {seat?.ssh ? <Text style={styles.mono}>{seat.ssh}</Text> : null}

          <Pressable style={styles.btn} onPress={probe}>
            <Text style={styles.btnText}>Find seat</Text>
          </Pressable>
          <Pressable
            style={[styles.btn, !base && styles.btnDisabled]}
            onPress={() => base && setPage("board")}
          >
            <Text style={styles.btnText}>Open seat</Text>
          </Pressable>
          <Pressable
            style={[styles.btnGhost, !base && styles.btnDisabled]}
            onPress={() => base && setPage("tty")}
          >
            <Text style={styles.btnGhostText}>Terminal only</Text>
          </Pressable>
        </ScrollView>
      </SafeAreaView>
    </View>
  );
}

const styles = StyleSheet.create({
  safe: { flex: 1, backgroundColor: PAPER, padding: 22 },
  eyebrow: {
    letterSpacing: 2,
    textTransform: "uppercase",
    color: SAGE,
    fontSize: 12,
    marginTop: 12,
  },
  title: { fontSize: 32, color: INK, marginTop: 6, fontWeight: "500" },
  lede: { color: "#5a4c3e", marginTop: 8, lineHeight: 22, fontSize: 16 },
  card: {
    marginTop: 22,
    padding: 14,
    borderWidth: 1,
    borderColor: "#d7cbb8",
    backgroundColor: "#fffaf3",
  },
  step: { color: INK, marginBottom: 8, lineHeight: 20 },
  label: { color: ESPRESSO, fontWeight: "600", marginTop: 16, marginBottom: 6 },
  input: {
    borderWidth: 1,
    borderColor: "#d7cbb8",
    backgroundColor: "#fffaf3",
    color: INK,
    paddingVertical: 10,
    paddingHorizontal: 12,
    fontSize: 15,
  },
  mono: { fontFamily: "monospace", color: INK, lineHeight: 20, fontSize: 13, marginTop: 8 },
  status: { marginTop: 16, color: SAGE, lineHeight: 20 },
  btn: {
    marginTop: 14,
    backgroundColor: ESPRESSO,
    paddingVertical: 14,
    alignItems: "center",
  },
  btnDisabled: { opacity: 0.4 },
  btnText: { color: "#fffaf3", fontSize: 16 },
  btnGhost: { marginTop: 10, paddingVertical: 12, alignItems: "center" },
  btnGhostText: { color: ESPRESSO, fontSize: 15 },
});
