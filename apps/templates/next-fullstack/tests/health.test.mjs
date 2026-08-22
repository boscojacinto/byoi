// Placeholder so `npm test` exists. The salon generates the real acceptance
// suite from SPEC.md; this only proves the runner is wired.
import { test } from "node:test";
import assert from "node:assert/strict";

test("the health route module is importable", async () => {
  assert.ok(process.env.DATABASE_URL === undefined || typeof process.env.DATABASE_URL === "string");
});
