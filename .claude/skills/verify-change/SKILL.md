---
name: verify-change
description: Verify a code change inside a guest seat, using only what the seat actually permits — its narrow Bash allowlist, plus the headless browser it reaches over MCP. Use before calling a brief done — checking a build, running lint, confirming tests pass, or looking at the rendered page — especially when the first attempt at curl, npx, or a direct node_modules/.bin binary got silently blocked with no prompt.
---

# Verifying a change on a seat

A guest seat runs Claude Code headless (`-p`), with a fixed, narrow Bash
allowlist (`apps/seat/claude_chat.py`, `DEFAULT_ALLOWED_TOOLS`). Two
consequences that don't show up in the interactive TUI:

* **Commands off the allowlist are denied by Claude Code's own Bash safety
  classifier before any control-request is ever emitted.** There is no
  prompt to answer — not for you, not for the operator. Retrying the same
  shape of command a different way will not help.
* The allowlist is deliberately narrow: build/test/install invocations only,
  nothing that reaches the network or the filesystem outside the project.
  **`curl`, `npx`, and calling a binary in `node_modules/.bin` directly are
  not on it and never will be**, even though the equivalent `npm run <script>`
  often is.

## What to actually run

`Bash(npm run *)` is already allowlisted — it covers *any* script name in
`package.json`. Before reaching for a raw tool, check what scripts already
exist:

```
cat package.json   # or: grep -A10 '"scripts"' package.json
```

Then use the npm/yarn/pnpm/bun script that does what you need, not the tool
underneath it:

| Want to check | Don't run | Run instead |
|---|---|---|
| Typecheck / compile | `npx tsc --noEmit` | `npm run build` (if it type-checks) or add/use a `typecheck` script |
| Lint | `./node_modules/.bin/eslint <file>` | `npm run lint` |
| Fetch the rendered page | `curl localhost:3000` | not available — see below |

Non-JS stacks: `pytest`, `python -m pytest`, `make`, `cargo build`/`test`,
`go build`/`test` are allowlisted directly, no wrapper script needed.

If the project has no script that does what you need (no `lint` entry, no
typecheck step), say so plainly in your summary instead of inventing a raw
command — it will be silently denied and you'll waste turns discovering that.

## Looking at the rendered page

`curl` is still blocked and always will be. There is a headless browser
instead, reached over MCP (`mcp__browser__*`) and already allowlisted, so it
does not prompt. Start the dev server in the background, then:

| Want to check | Call |
|---|---|
| The page loads at all | `browser_navigate` → `http://localhost:3000` |
| What is on it, and what you can click | `browser_snapshot` |
| How it actually looks | `browser_take_screenshot` |
| A runtime error the build could not catch | `browser_console_messages` |
| A request that 404s or never fires | `browser_network_requests` |

**Reach for `browser_snapshot` first.** It returns the accessibility tree as
text: cheaper than a screenshot, and the only one of the two you can act on,
since clicking needs an element reference that a snapshot gives you and a
picture does not.

**Take pixels when the question is about pixels** — spacing, overflow, colour,
"does this look right". The guest sees every screenshot in their own chat, so
one at the end of a UI change is worth taking even when you are already sure.
Don't take them in a loop: a seat runs on a pooled account that fails over on
quota, and screenshots are the expensive thing in this sandbox.

`npm run dev` blocks, so start it with `run_in_background` and give it a few
seconds before the first navigate.

If `mcp__browser__*` is not in your tool list, this seat was built without a
browser. Say so rather than guessing at how the page looks.

## What you still cannot verify from a seat

Nothing about the guest's own device: how the page renders in *their* browser,
at their screen size, with their fonts. What you have is one headless Chromium
at one viewport. For anything device-specific, point them at the address under
**This session** in their app — it opens the same dev server on their phone —
and ask them what they see.

Where you cannot see the result at all, fall back to:

1. A clean `npm run build` (or equivalent) — catches syntax/type errors.
2. `npm run lint` if defined.
3. Reading the code you changed against the code it depends on — e.g. for a
   CSS animation, check the keyframe percentages actually match how many
   copies of the content you render, rather than assuming.

Then tell the guest explicitly what wasn't checked and why, rather than
implying it was.
