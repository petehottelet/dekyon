---
title: "Fixed auth token refresh window"
date: 2026-08-17T14:32-07:00
session_id: "a1b2c3d4"
project: "billing-service"
cwd: "/home/dev/billing-service"
branch: "fix/auth"
reason: "prompt_input_exit"
kind: "session"
model: "claude-haiku"
duration_min: 24
messages: {user: 4, assistant: 6}
---

# Fixed auth token refresh window

## What happened
- Diagnosed premature token expiry; access tokens were minted with a 60 s TTL
  instead of 3600 s, so every request after the first minute 401'd.
- Patched the refresh window and added a regression path.

## Decisions
- Keep refresh logic server-side; rejected client-side token caching to avoid
  storing credentials in the browser.

## Changes
- `src/auth/refresh.py` - TTL 60 -> 3600, added guard for clock skew.

## Lessons
- refresh.py expiry values are in seconds, not milliseconds.
- pytest here needs `-q` or CI truncates the failure output.

## Open threads
- Add a regression test for clock skew across timezones.

---
*Auto-captured by dekyon . tools: Edit x2, Bash x3, Read x4 . files touched: 1*
