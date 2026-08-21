export function normalizeBase(raw) {
  let s = (raw || "").trim();
  if (!s) return "";
  if (!/^[a-zA-Z][a-zA-Z0-9+.-]*:/.test(s)) s = "https://" + s;
  return s.replace(/\/$/, "");
}

export function parseJoinUrl(raw) {
  const s = (raw || "").trim();
  if (!s) return null;
  let url;
  try {
    url = new URL(/^[a-zA-Z][a-zA-Z0-9+.-]*:/.test(s) ? s : `https://${s}`);
  } catch {
    return null;
  }
  const otp = url.searchParams.get("otp") || "";
  if (url.protocol === "byoi:") {
    const host = url.searchParams.get("host") || url.searchParams.get("seat") || "";
    return { base: normalizeBase(host), otp };
  }
  if (url.protocol === "http:" || url.protocol === "https:") {
    return { base: `${url.protocol}//${url.host}`, otp };
  }
  return null;
}

export function guessSeatBase(constants) {
  const c = constants || {};
  const candidates = [c.expoConfig?.hostUri, c.expoGo?.debuggerHost, c.linkingUri].filter(Boolean);
  for (const raw of candidates) {
    const m = String(raw).match(/(\d{1,3}(?:\.\d{1,3}){3})/);
    if (m && m[1] !== "127.0.0.1") return `https://${m[1]}:8787`;
  }
  return "";
}
