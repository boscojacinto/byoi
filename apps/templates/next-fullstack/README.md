# BYOI salon starter — Next.js + Postgres + Redis + auth

A project that already has a data layer, so a guest can spend their session on
the brief instead of on plumbing.

## The URL contract

The app reads `DATABASE_URL`, `REDIS_URL`, and `AUTH_SECRET` and never cares
where they point:

| Where | Who sets them |
|---|---|
| Seat PC, during the session | `docker compose` stack; written into `.env.local` |
| Vercel preview, on deploy | Managed Postgres/Redis, injected by the desk |

That is the whole trick: the same code is developed, tested, and deployed
without an `if (production)` anywhere.

## On the seat

The stack starts when the host claims the brief. To do it by hand:

```bash
npm install
npm run db:init
npm run dev
```

## Routes

| Route | What it exercises |
|---|---|
| `GET /api/health` | Real round-trip to Postgres and Redis, plus auth config |
| `POST /api/auth` | Issues a signed session cookie |
| `GET/POST /api/notes` | Authenticated, cached, persisted resource |

`lib/auth.ts` is a deliberately small signed-cookie scheme. Swap it for Auth.js
or Clerk if the brief needs real providers — the route contract does not change.

`SPEC.md` is the acceptance spec for this template; paste it into the brief and
the desk will generate a suite from it.
