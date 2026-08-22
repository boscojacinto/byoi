// Idempotent schema bootstrap. Runs on the seat against local Postgres and on
// the host against the managed one, from the same DATABASE_URL contract.
import pg from "pg";

const url = process.env.DATABASE_URL;
if (!url) {
  console.error("DATABASE_URL is not set");
  process.exit(1);
}
const client = new pg.Client({
  connectionString: url,
  ssl: url.includes("127.0.0.1") ? undefined : { rejectUnauthorized: false },
});
await client.connect();
await client.query(`
  create table if not exists notes (
    id serial primary key,
    user_id text not null,
    body text not null,
    created_at timestamptz not null default now()
  )
`);
await client.query("create index if not exists notes_user_id_idx on notes (user_id)");
await client.end();
console.log("schema ready");
