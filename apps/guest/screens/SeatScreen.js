import { useCallback, useEffect, useRef, useState } from "react";
import {
  BackHandler,
  Linking,
  Pressable,
  SafeAreaView,
  Share,
  StatusBar,
  Text,
  View,
} from "react-native";
import { WebView } from "react-native-webview";
import { useKeepAwake } from "expo-keep-awake";
import { PAPER, styles } from "../theme";

// Ask the page to take one step back — close the console, close a sheet, leave
// the chat for the floor — and tell us when it had nothing left to close, so
// the hardware key can mean "leave the seat" only at the end of that chain.
const BACK = `
  (function () {
    var handled = window.byoiBack && window.byoiBack();
    if (!handled) window.ReactNativeWebView.postMessage(JSON.stringify({ type: "exit" }));
  })();
  true;
`;

// Everything a guest does at a seat is the seat's own PWA — the floor, the
// solution board, the chat, the spec results — so the app carries no second
// copy of any of it to keep in step. What is native here is only what a page
// on a phone cannot do: trust the salon CA, read the slip QR with the camera,
// and answer the Android back key.
export default function SeatScreen({ base, otp, onLeave }) {
  useKeepAwake();
  const web = useRef(null);
  const [error, setError] = useState("");
  const [attempt, setAttempt] = useState(0);
  const uri = `${base}/guest/${otp ? `?otp=${encodeURIComponent(otp)}` : ""}`;

  useEffect(() => {
    const sub = BackHandler.addEventListener("hardwareBackPress", () => {
      if (error) {
        onLeave();
        return true;
      }
      web.current?.injectJavaScript(BACK);
      return true;
    });
    return () => sub.remove();
  }, [error, onLeave]);

  const onMessage = useCallback(
    async (event) => {
      let msg;
      try {
        msg = JSON.parse(event.nativeEvent.data);
      } catch {
        return;
      }
      if (msg.type === "exit") {
        onLeave();
        return;
      }
      // A transcript or a handoff summary. There is nowhere on a phone to put
      // a downloaded file where the guest would find it again, so offer it to
      // whatever they already keep notes in.
      if (msg.type === "save" && msg.body) {
        try {
          await Share.share({ message: String(msg.body), title: msg.name || "BYOI" });
        } catch {
          // They dismissed the sheet. The chat already said the file was ready.
        }
      }
    },
    [onLeave]
  );

  // The preview at :3000, a deployed URL, and Claude's own sign-in page are all
  // off this seat's origin. They belong in the phone's browser — a guest who
  // opened Claude sign-in inside this WebView would have no way back.
  const onRequest = useCallback(
    (req) => {
      const url = req.url || "";
      if (url.startsWith(base) || url.startsWith("about:")) return true;
      if (/^https?:/i.test(url)) {
        Linking.openURL(url).catch(() => {});
        return false;
      }
      return false;
    },
    [base]
  );

  if (error) {
    return (
      <View style={styles.root}>
        <StatusBar barStyle="dark-content" backgroundColor={PAPER} />
        <SafeAreaView style={styles.safe}>
          <Text style={styles.eyebrow}>BYOI</Text>
          <Text style={styles.title}>Seat unreachable</Text>
          <Text style={styles.lede}>{error}</Text>
          <Text style={styles.mono}>{uri}</Text>
          <Pressable
            style={styles.btn}
            onPress={() => {
              setError("");
              setAttempt((n) => n + 1);
            }}
          >
            <Text style={styles.btnText}>Try again</Text>
          </Pressable>
          <Pressable style={styles.btnGhost} onPress={onLeave}>
            <Text style={styles.btnGhostText}>Leave seat</Text>
          </Pressable>
        </SafeAreaView>
      </View>
    );
  }

  return (
    <View style={styles.root}>
      <StatusBar barStyle="dark-content" backgroundColor={PAPER} />
      <SafeAreaView style={{ flex: 1, backgroundColor: PAPER }}>
        <WebView
          key={attempt}
          ref={web}
          source={{ uri }}
          style={{ flex: 1, backgroundColor: PAPER }}
          originWhitelist={["*"]}
          javaScriptEnabled
          domStorageEnabled
          mixedContentMode="always"
          setSupportMultipleWindows={false}
          onMessage={onMessage}
          onShouldStartLoadWithRequest={onRequest}
          onError={({ nativeEvent }) =>
            setError(nativeEvent.description || "Same Wi-Fi as the seat PC? Seat agent on :8787?")
          }
          onHttpError={({ nativeEvent }) =>
            setError(`The seat answered ${nativeEvent.statusCode}. Ask the host to check it.`)
          }
          // The chat composer sits on the keyboard, and the guest scrolls a long
          // log — neither wants a bounce or an overscroll glow underneath it.
          overScrollMode="never"
          bounces={false}
          keyboardDisplayRequiresUserAction={false}
          hideKeyboardAccessoryView
          allowsInlineMediaPlayback
          mediaCapturePermissionGrantType="grant"
          contentInsetAdjustmentBehavior="never"
        />
      </SafeAreaView>
    </View>
  );
}
