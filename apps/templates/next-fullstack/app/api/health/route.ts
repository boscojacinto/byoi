// Liveness plus a real round-trip to both backends. The salon's deployed smoke
// test hits exactly this route, so keep it honest — no faking "ok".
import { query } from "@/lib/db";
import { cache } from "@/lib/cache";

export const dynamic = "force-dynamic";

export async function GET() {
  const checks: Record<string, string> = {};
  let ok = true;
  try {
    await query("select 1");
    checks.database = "ok";
  } catch (err) {
    ok = false;
    checks.database = (err as Error).message;
  }
  try {
    const client = await cache();
    await client.set("byoi:health", String(Date.now()));
    checks.cache = (await client.get("byoi:health")) ? "ok" : "no value read back";
  } catch (err) {
    ok = false;
    checks.cache = (err as Error).message;
  }
  checks.auth = process.env.AUTH_SECRET ? "ok" : "AUTH_SECRET is not set";
  if (checks.auth !== "ok") ok = false;
  return Response.json({ ok, checks }, { status: ok ? 200 : 503 });
}
