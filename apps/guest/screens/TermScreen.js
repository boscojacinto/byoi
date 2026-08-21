import { Pressable, SafeAreaView, StatusBar, Text, View } from "react-native";
import { WebView } from "react-native-webview";
import { useKeepAwake } from "expo-keep-awake";
import { PAPER, styles } from "../theme";

export default function TermScreen({ base, ticket, onClose }) {
  useKeepAwake();
  const q = ticket ? `?ticket=${encodeURIComponent(ticket)}` : "";
  return (
    <View style={styles.root}>
      <StatusBar barStyle="light-content" backgroundColor="#1b1612" />
      <SafeAreaView style={{ flex: 1, backgroundColor: "#1b1612" }}>
        <Pressable style={styles.btnGhost} onPress={onClose}>
          <Text style={[styles.btnGhostText, { color: PAPER }]}>Leave TTY</Text>
        </Pressable>
        <WebView
          source={{ uri: `${base}/tty${q}` }}
          originWhitelist={["*"]}
          javaScriptEnabled
          domStorageEnabled
          mixedContentMode="always"
          setSupportMultipleWindows={false}
          style={{ flex: 1, backgroundColor: "#1b1612" }}
        />
      </SafeAreaView>
    </View>
  );
}
