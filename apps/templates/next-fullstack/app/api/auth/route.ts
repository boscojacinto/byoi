import { SESSION_COOKIE, sign } from "@/lib/auth";

export const dynamic = "force-dynamic";

export async function POST(request: Request) {
  const { user } = (await request.json()) as { user?: string };
  if (!user?.trim()) return Response.json({ error: "user is required" }, { status: 400 });
  const res = Response.json({ user: user.trim() });
  res.headers.append(
    "set-cookie",
    `${SESSION_COOKIE}=${sign(user.trim())}; Path=/; HttpOnly; SameSite=Lax`,
  );
  return res;
}
