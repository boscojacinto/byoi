// Deliberately tiny: a signed session cookie over AUTH_SECRET. Swap for Auth.js
// or Clerk if the brief calls for real providers — the route contract is the same.
import { createHmac, timingSafeEqual } from "node:crypto";

const COOKIE = "byoi_session";

function secret(): string {
  const value = process.env.AUTH_SECRET;
  if (!value) throw new Error("AUTH_SECRET is not set");
  return value;
}

export function sign(userId: string): string {
  const body = Buffer.from(JSON.stringify({ sub: userId, iat: Date.now() })).toString("base64url");
  const mac = createHmac("sha256", secret()).update(body).digest("base64url");
  return `${body}.${mac}`;
}

export function verify(token: string | undefined): { sub: string } | null {
  if (!token || !token.includes(".")) return null;
  const [body, mac] = token.split(".");
  const expected = createHmac("sha256", secret()).update(body).digest("base64url");
  const a = Buffer.from(mac);
  const b = Buffer.from(expected);
  if (a.length !== b.length || !timingSafeEqual(a, b)) return null;
  try {
    return JSON.parse(Buffer.from(body, "base64url").toString()) as { sub: string };
  } catch {
    return null;
  }
}

export const SESSION_COOKIE = COOKIE;
