---
name: verify-change
description: Verify a code change inside a guest seat, using only commands the seat's Bash allowlist actually permits. Use before calling a brief done — checking a build, running lint, confirming tests pass — especially when the first attempt at curl, npx, or a direct node_modules/.bin binary got silently blocked with no prompt.
---

# Verifying a change on a seat

A guest seat runs Claude Code headless (`-p`), with a fixed, narrow Bash
allowlist (`apps/seat/claude_chat.py:132-140`, `DEFAULT_ALLOWED_TOOLS`). Two
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

## What you cannot verify from a seat

There is no browser automation (no Playwright, no `chromium-cli`) installed
in the guest sandbox, and fetching localhost with `curl` is blocked by
design. Visual confirmation of a rendered page is **not achievable** from
inside a seat today. For UI/CSS/animation changes, verify by:

1. A clean `npm run build` (or equivalent) — catches syntax/type errors.
2. `npm run lint` if defined.
3. Reading the code you changed against the code it depends on — e.g. for a
   CSS animation, check the keyframe percentages actually match how many
   copies of the content you render, rather than assuming.

Then tell the guest explicitly that visual rendering wasn't checked and why,
rather than implying it was.
