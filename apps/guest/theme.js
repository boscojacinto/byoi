import { StyleSheet } from "react-native";

export const PAPER = "#f4efe4";
export const INK = "#2b2118";
export const ESPRESSO = "#5c3317";
export const SAGE = "#6b7f6a";
export const FOAM = "#fffaf3";
export const RULE = "#d7cbb8";

export const styles = StyleSheet.create({
  root: { flex: 1, backgroundColor: PAPER },
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
    borderColor: RULE,
    backgroundColor: FOAM,
  },
  step: { color: INK, marginBottom: 8, lineHeight: 20 },
  label: { color: ESPRESSO, fontWeight: "600", marginTop: 16, marginBottom: 6 },
  input: {
    borderWidth: 1,
    borderColor: RULE,
    backgroundColor: FOAM,
    color: INK,
    paddingVertical: 10,
    paddingHorizontal: 12,
    fontSize: 15,
  },
  mono: { fontFamily: "monospace", color: INK, lineHeight: 20, fontSize: 13, marginTop: 8 },
  status: {
    marginTop: 16,
    color: SAGE,
    lineHeight: 20,
    borderWidth: 1,
    borderColor: RULE,
    backgroundColor: FOAM,
    paddingVertical: 10,
    paddingHorizontal: 12,
  },
  btn: {
    marginTop: 14,
    backgroundColor: ESPRESSO,
    paddingVertical: 14,
    alignItems: "center",
  },
  btnDisabled: { opacity: 0.4 },
  btnText: { color: FOAM, fontSize: 16 },
  btnGhost: { marginTop: 10, paddingVertical: 12, alignItems: "center" },
  btnGhostText: { color: ESPRESSO, fontSize: 15 },
  scanHint: {
    position: "absolute",
    bottom: 36,
    left: 22,
    right: 22,
    alignItems: "center",
  },
});
