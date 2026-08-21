import { useState } from "react";
import { Pressable, StyleSheet, Text, View } from "react-native";
import { CameraView, useCameraPermissions } from "expo-camera";
import { FOAM, PAPER, styles } from "../theme";

export default function ScanScreen({ onCancel, onCode }) {
  const [permission, requestPermission] = useCameraPermissions();
  const [locked, setLocked] = useState(false);

  if (!permission) {
    return <View style={styles.root} />;
  }

  if (!permission.granted) {
    return (
      <View style={styles.safe}>
        <Text style={styles.title}>Camera</Text>
        <Text style={styles.lede}>BYOI Guest needs the camera to read the check-in slip QR.</Text>
        <Pressable style={styles.btn} onPress={requestPermission}>
          <Text style={styles.btnText}>Allow camera</Text>
        </Pressable>
        <Pressable style={styles.btnGhost} onPress={onCancel}>
          <Text style={styles.btnGhostText}>Cancel</Text>
        </Pressable>
      </View>
    );
  }

  return (
    <View style={local.fill}>
      <CameraView
        style={local.fill}
        facing="back"
        barcodeScannerSettings={{ barcodeTypes: ["qr"] }}
        onBarcodeScanned={({ data }) => {
          if (locked) return;
          setLocked(true);
          onCode(data);
        }}
      />
      <View style={styles.scanHint}>
        <Text style={local.hint}>Point at the slip QR</Text>
        <Pressable style={styles.btnGhost} onPress={onCancel}>
          <Text style={[styles.btnGhostText, { color: FOAM }]}>Cancel</Text>
        </Pressable>
      </View>
    </View>
  );
}

const local = StyleSheet.create({
  fill: { flex: 1, backgroundColor: "#1b1612" },
  hint: { color: PAPER, fontSize: 16, marginBottom: 8 },
});
