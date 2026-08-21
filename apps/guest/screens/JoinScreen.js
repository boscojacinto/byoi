import { Pressable, SafeAreaView, ScrollView, StatusBar, Text, TextInput, View } from "react-native";
import { ESPRESSO, PAPER, styles } from "../theme";

export default function JoinScreen({
  host,
  otp,
  status,
  busy,
  onChangeHost,
  onChangeOtp,
  onScan,
  onSit,
}) {
  return (
    <View style={styles.root}>
      <StatusBar barStyle="dark-content" backgroundColor={PAPER} />
      <SafeAreaView style={styles.safe}>
        <ScrollView contentContainerStyle={{ paddingBottom: 40 }} keyboardShouldPersistTaps="handled">
          <Text style={styles.eyebrow}>BYOI guest</Text>
          <Text style={styles.title}>Sit. Same Wi-Fi. Attach.</Text>
          <Text style={styles.lede}>
            This is the vibe-coder app. Join the cafe Wi-Fi, scan the slip, claim a
            brief, attach the seat TTY. No browser, no Bluetooth.
          </Text>

          <View style={styles.card}>
            <Text style={styles.step}>1. Same Wi-Fi as the seat PC.</Text>
            <Text style={styles.step}>2. Scan the check-in QR (or paste the join URL).</Text>
            <Text style={styles.step}>3. Claim a brief, then attach tmux claude-guest.</Text>
          </View>

          <Pressable style={styles.btn} onPress={onScan} disabled={busy}>
            <Text style={styles.btnText}>Scan slip QR</Text>
          </Pressable>

          <Text style={styles.label}>Join URL or seat address</Text>
          <TextInput
            style={styles.input}
            value={host}
            onChangeText={onChangeHost}
            placeholder="https://192.168.x.x:8787/join?otp=…"
            placeholderTextColor="#9a8b7a"
            autoCapitalize="none"
            autoCorrect={false}
            keyboardType="url"
          />
          <Text style={styles.label}>OTP</Text>
          <TextInput
            style={styles.input}
            value={otp}
            onChangeText={onChangeOtp}
            placeholder="from the slip, if not in the URL"
            placeholderTextColor="#9a8b7a"
            autoCapitalize="none"
            autoCorrect={false}
          />

          {status ? <Text style={styles.status}>{status}</Text> : null}

          <Pressable
            style={[styles.btn, busy && styles.btnDisabled]}
            onPress={onSit}
            disabled={busy}
          >
            <Text style={styles.btnText}>{busy ? "Sitting…" : "Sit at this seat"}</Text>
          </Pressable>
          <Text style={[styles.lede, { marginTop: 18, color: ESPRESSO }]}>
            Scan is the floor path. The URL box is a fallback if the camera cannot
            read the slip.
          </Text>
        </ScrollView>
      </SafeAreaView>
    </View>
  );
}
