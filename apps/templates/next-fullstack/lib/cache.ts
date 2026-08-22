// Redis. Same URL contract as the database: local on the seat, managed on deploy.
import { createClient, type RedisClientType } from "redis";

declare global {
  // eslint-disable-next-line no-var
  var __byoiRedis: RedisClientType | undefined;
}

export async function cache(): Promise<RedisClientType> {
  if (!process.env.REDIS_URL) {
    throw new Error("REDIS_URL is not set — run the seat's infra stack or deploy");
  }
  if (!globalThis.__byoiRedis) {
    const client: RedisClientType = createClient({ url: process.env.REDIS_URL });
    client.on("error", () => {});
    await client.connect();
    globalThis.__byoiRedis = client;
  }
  return globalThis.__byoiRedis;
}
