import { Pressable, SafeAreaView, ScrollView, StatusBar, Text, View } from "react-native";
import { PAPER, styles } from "../theme";

export default function FloorScreen({
  join,
  status,
  busy,
  onClaim,
  onAttach,
  onShipped,
  onLeave,
}) {
  const session = join?.session;
  const seat = join?.seat;
  const board = join?.board || [];
  const claimed = join?.item;
  const hello = session
    ? `${seat?.name || "Seat"} · hello ${session.coder_name}`
    : "Checked in at the desk? Scan the slip QR.";

  return (
    <View style={styles.root}>
      <StatusBar barStyle="dark-content" backgroundColor={PAPER} />
      <SafeAreaView style={styles.safe}>
        <ScrollView contentContainerStyle={{ paddingBottom: 40 }}>
          <Text style={styles.eyebrow}>vibe coder</Text>
          <Text style={styles.title}>{seat?.name || "Seat"}</Text>
          <Text style={styles.lede}>{hello}</Text>
          {join?.wifi_ssid ? (
            <Text style={styles.pill}>Wi-Fi · {join.wifi_ssid}</Text>
          ) : null}
          {session ? (
            <Text style={styles.mono}>
              tmux {seat?.claude_label || "claude-guest"} · otp {session.unlock_otp}
            </Text>
          ) : null}

          {claimed ? (
            <View style={styles.card}>
              <Text style={styles.label}>This session</Text>
              <Text style={styles.briefTitle}>{claimed.title}</Text>
              <Text style={styles.briefBody}>{claimed.brief}</Text>
              <Text style={styles.pill}>
                {claimed.wellness_minutes} min · break {claimed.break_after}
              </Text>
            </View>
          ) : null}

          {status ? <Text style={styles.status}>{status}</Text> : null}

          <Pressable
            style={[styles.btn, (busy || !session) && styles.btnDisabled]}
            onPress={onAttach}
            disabled={busy || !session}
          >
            <Text style={styles.btnText}>Attach TTY</Text>
          </Pressable>
          <Pressable style={styles.btnGhost} onPress={onLeave}>
            <Text style={styles.btnGhostText}>Leave seat</Text>
          </Pressable>

          <Text style={[styles.label, { marginTop: 28 }]}>Solution board</Text>
          {board.map((item) => (
            <View key={item.id} style={styles.brief}>
              <Text style={styles.briefTitle}>{item.title}</Text>
              <Text style={styles.briefBody}>{item.brief}</Text>
              <Text style={styles.pill}>
                {item.wellness_minutes} min · break at {item.break_after}
              </Text>
              <Pressable
                style={[styles.claim, (!session || busy) && styles.btnDisabled]}
                onPress={() => onClaim(item.id)}
                disabled={!session || busy}
              >
                <Text style={styles.claimText}>Claim this brief</Text>
              </Pressable>
            </View>
          ))}

          {session ? (
            <Pressable
              style={[styles.btnGhost, busy && styles.btnDisabled]}
              onPress={onShipped}
              disabled={busy}
            >
              <Text style={styles.btnGhostText}>Mark shipped</Text>
            </Pressable>
          ) : null}
        </ScrollView>
      </SafeAreaView>
    </View>
  );
}
