import { Pressable, SafeAreaView, StatusBar, Text, View } from "react-native";
import { WebView } from "react-native-webview";
import { useKeepAwake } from "expo-keep-awake";
import { PAPER, styles } from "../theme";

export default function ChatScreen({ base, ticket, otp, onClose }) {
  useKeepAwake();
  const params = new URLSearchParams({ view: "chat", embedded: "1" });
  if (ticket) params.set("ticket", ticket);
  if (otp) params.set("otp", otp);
  const q = `?${params.toString()}`;
  return (
    <View style={styles.root}>
      <StatusBar barStyle="dark-content" backgroundColor={PAPER} />
      <SafeAreaView style={{ flex: 1, backgroundColor: PAPER }}>
        <Pressable style={styles.btnGhost} onPress={onClose}>
          <Text style={styles.btnGhostText}>Leave chat</Text>
        </Pressable>
        <WebView
          source={{ uri: `${base}/guest/${q}` }}
          originWhitelist={["*"]}
          javaScriptEnabled
          domStorageEnabled
          mixedContentMode="always"
          setSupportMultipleWindows={false}
          keyboardDisplayRequiresUserAction={false}
          hideKeyboardAccessoryView
          style={{ flex: 1, backgroundColor: PAPER }}
        />
      </SafeAreaView>
    </View>
  );
}
