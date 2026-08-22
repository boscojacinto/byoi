// Postgres. DATABASE_URL is local Docker on the seat and managed on a deploy —
// the app never needs to know which.
import { Pool } from "pg";

declare global {
  // eslint-disable-next-line no-var
  var __byoiPool: Pool | undefined;
}

export function pool(): Pool {
  if (!process.env.DATABASE_URL) {
    throw new Error("DATABASE_URL is not set — run the seat's infra stack or deploy");
  }
  if (!globalThis.__byoiPool) {
    globalThis.__byoiPool = new Pool({
      connectionString: process.env.DATABASE_URL,
      // Managed Postgres needs TLS; the seat's local container does not.
      ssl: process.env.DATABASE_URL.includes("127.0.0.1")
        ? undefined
        : { rejectUnauthorized: false },
      max: 5,
    });
  }
  return globalThis.__byoiPool;
}

export async function query<T = unknown>(text: string, params: unknown[] = []) {
  const res = await pool().query(text, params);
  return res.rows as T[];
}
