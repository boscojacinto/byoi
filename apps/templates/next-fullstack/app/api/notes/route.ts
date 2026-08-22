// A tiny authenticated, cached, persisted resource — the smallest thing that
// exercises all three pieces of infrastructure at once.
import { cookies } from "next/headers";
import { query } from "@/lib/db";
import { cache } from "@/lib/cache";
import { SESSION_COOKIE, verify } from "@/lib/auth";

export const dynamic = "force-dynamic";

async function currentUser() {
  const jar = await cookies();
  return verify(jar.get(SESSION_COOKIE)?.value);
}

export async function GET() {
  const user = await currentUser();
  if (!user) return Response.json({ error: "unauthorized" }, { status: 401 });

  const key = `notes:${user.sub}`;
  const client = await cache();
  const hit = await client.get(key);
  if (hit) return Response.json({ notes: JSON.parse(hit), cached: true });

  const notes = await query<{ id: number; body: string }>(
    "select id, body from notes where user_id = $1 order by id desc",
    [user.sub],
  );
  await client.set(key, JSON.stringify(notes), { EX: 30 });
  return Response.json({ notes, cached: false });
}

export async function POST(request: Request) {
  const user = await currentUser();
  if (!user) return Response.json({ error: "unauthorized" }, { status: 401 });

  const { body } = (await request.json()) as { body?: string };
  if (!body?.trim()) return Response.json({ error: "body is required" }, { status: 400 });

  const [note] = await query<{ id: number; body: string }>(
    "insert into notes (user_id, body) values ($1, $2) returning id, body",
    [user.sub, body.trim()],
  );
  const client = await cache();
  await client.del(`notes:${user.sub}`);
  return Response.json({ note }, { status: 201 });
}
