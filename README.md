<div align="center">
  <img src="assets/dekyon_logo_v4_animated.svg" alt="dekyon — close the loop. keep the memory." width="680">

  <p>
    <a href="https://skills.sh/petehottelet/dekyon"><img src="https://skills.sh/b/petehottelet/dekyon?style=flat-square&amp;labelColor=001827&amp;color=00ACFF" alt="dekyon installs on skills.sh"></a>
    <a href="https://github.com/petehottelet/dekyon/actions/workflows/ci.yml"><img src="https://img.shields.io/github/actions/workflow/status/petehottelet/dekyon/ci.yml?branch=main&amp;style=flat-square&amp;label=CI&amp;labelColor=001827&amp;logo=githubactions&amp;logoColor=00ACFF" alt="CI status"></a>
    <img src="https://img.shields.io/badge/python-3.8%2B-00ACFF?style=flat-square&amp;labelColor=001827&amp;logo=python&amp;logoColor=white" alt="Python 3.8 or newer">
    <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-00C2C7?style=flat-square&amp;labelColor=001827" alt="License: MIT"></a>
    <a href="https://github.com/petehottelet/dekyon/releases/latest"><img src="https://img.shields.io/github/v/release/petehottelet/dekyon?style=flat-square&amp;label=release&amp;labelColor=001827&amp;color=6C63FF" alt="Latest release"></a>
  </p>

  <p>
    <a href="#install">Install</a> ·
    <a href="#what-dekyon-data-looks-like">Example output</a> ·
    <a href="#everyday-use">Everyday use</a> ·
    <a href="#how-it-works-and-why">How it works</a>
  </p>
</div>

**Automatic session memory for coding agents, stored as markdown in a git
repo you own.**

When a Claude Code or Codex CLI session ends, dekyon captures the
transcript, writes one narrative note - what happened, what was decided,
what changed, what was learned, what's still open - and commits + pushes it
to a private GitHub repo using your normal git credentials. With recall
enabled, the next session gets the signal and picks up where you left off.

Remember three things: what changed, what was learned, what remains.

No resident daemon, no database, no background service, no open ports. Your
memory is plain markdown files - in a repo *you* own - that you can read,
grep, diff, and delete like anything else in git. With the default AI
summarizer, dekyon sends a redacted session digest through your authenticated
`claude` CLI with built-in tools, MCP tools, skills, and session persistence
disabled; set `summarizer` to `"none"` for a fully local structural digest.
Git sync happens only when `push` is enabled.

> **Why not a memory database?** Because you can't `cat` a database, can't
> review it in a PR, and can't be sure what it's sending where. dekyon's
> entire storage layer is files in your git repo. The tool is a small Python
> core and a few hooks - the only thing to trust is code you can
> read in an afternoon.

```
session ends ──▶ hook spools payload, exits in ~60 ms
                   └──▶ detached worker (outlives the agent):
                          parse transcript ─▶ AI summary (or structural fallback)
                          ─▶ write sessions/<project>/<date>--<slug>--<id>.md
                          ─▶ git commit ─▶ git push (best-effort)
```

## Does it work with Claude? With Codex / ChatGPT?

| Environment | Automatic capture | Recall |
| --- | --- | --- |
| **Claude Code** (terminal / IDE) | ✅ Full: notes at SessionEnd, optional context injection at SessionStart, optional pre-compaction checkpoints, `/dekyon:checkpoint` command | ✅ Bundled skill: "where did we leave off?", "search my session notes" |
| **OpenAI Codex CLI / desktop** | ✅ Via Codex SessionEnd hooks, with crash-tolerant Stop upserts (details below) | ✅ Context injection at SessionStart |
| **claude.ai chat / Claude desktop** | ❌ No lifecycle hooks or access to your local filesystem, so nothing can fire when a chat ends | ➖ Notes are plain markdown on GitHub - browse them, or point any assistant at the repo |
| **ChatGPT (web / app)** | ❌ Same reason | ➖ Same |

In short: **capture needs a locally running coding agent with hooks**. The
notes themselves are portable markdown that any tool - or any human - can
read.

## What Dekyon Data looks like

```
sessions/
  my-project/
    index.md          ← newest-first log, one line per session
    lessons.md        ← rolling ledger of durable takeaways
    2026-08-17--1432--fixed-auth-token-refresh-window--a1b2c3d4.md
```

```markdown
---
title: "Fixed auth token refresh window"
date: 2026-08-17T14:32-07:00
session_id: "a1b2c3d4"
project: "my-project"
cwd: "/home/dev/my-project"
branch: "fix/auth"
reason: "prompt_input_exit"
kind: "session"
model: "claude-haiku"
duration_min: 24
messages: {user: 4, assistant: 6}
---

# Fixed auth token refresh window

## What happened
- Diagnosed premature token expiry; patched refresh window from 60 s to 3600 s.
## Decisions
- Keep refresh logic server-side; no client caching.
## Changes
- `src/auth/refresh.py`
## Lessons
- refresh.py expiry values are seconds, not ms
## Open threads
- add regression test for clock skew
```

Summaries come from a cheap, tool-free `claude -p --model haiku` call. If the
`claude` CLI is missing, times out, or errors, the worker falls back to a
deterministic structural digest (first prompt, files touched, commands run).
For every eligible session with a readable transcript, a note is still
written even when the AI call fails. `## Lessons` bullets are also appended to
the project's `lessons.md`, and the session-start injector shows the last
few, so hard-won environment quirks stop being relearned.

See [`examples/`](examples/) for a full sample notes tree you can browse
without installing anything.

## Install

**Requirements:** Python 3.8+ and `git` on PATH (macOS, Linux, WSL, or
Windows), plus git auth if you want to push to a private GitHub repo. On
Windows, use `python` or `py -3` wherever the examples show `python3`.

### Skill-only install (recall and checkpoints)

```bash
npx skills add petehottelet/dekyon --skill dekyon -g
```

This installs the portable skill for searching existing notes and writing
manual checkpoints. It requires a Node/npm version supported by the current
`skills` CLI. For automatic capture, install the plugin or hooks below.

### 1. Create the notes repo (once)

```bash
cd ~
gh repo create claude-session-notes --private --clone
# or manually:
#   git init -b main ~/claude-session-notes
#   git -C ~/claude-session-notes remote add origin git@github.com:YOU/claude-session-notes.git
```

> **Make it private.** Notes are derived from transcripts and can reference
> proprietary code. dekyon redacts obvious secret patterns (GitHub / AWS /
> Slack / API tokens, PEM blocks) before summarizing or writing, but
> redaction is best-effort, not a guarantee.

No remote yet? Commits stay local, the log tells you the exact
`git remote add` to run, and the next push carries the backlog. Repo
somewhere else? Set `repo_dir` in `~/.claude/dekyon.json` (auto-created
with defaults on first run).

### 2. Claude Code - as a plugin (recommended)

**From the marketplace** (once the repo is public):

```bash
# inside Claude Code:
/plugin marketplace add petehottelet/dekyon
/plugin install dekyon@dekyon
/reload-plugins
```

**Or test the complete plugin from a local checkout:**

```bash
git clone https://github.com/petehottelet/dekyon.git
claude --plugin-dir ./dekyon
```

`--plugin-dir` loads the repository as a plugin for that Claude Code session.
Validate a checkout with `claude plugin validate /path/to/dekyon`.
Verify with `/hooks` (SessionEnd should list a dekyon entry). The plugin
also gives you the `dekyon` recall/checkpoint skill and the
`/dekyon:checkpoint` command.

For a manual **skill-only** install without npm, copy the actual skill folder:

```bash
git clone https://github.com/petehottelet/dekyon.git
mkdir -p ~/.claude/skills
cp -r dekyon/skills/dekyon ~/.claude/skills/dekyon
```

The marketplace plugin's conservative default is capture-only. For the full
memory loop (startup recall plus pre-compaction checkpoints), use the
skill-only install together with the plain-hook installer below. Do not also
install the marketplace plugin, or SessionEnd would be registered twice.
(`hooks/hooks.context.json` in this repo documents that full-loop hook set;
`install.py --with-context --with-precompact` wires the equivalent into your
user settings.)

Injection costs a few hundred tokens per session start; it does a fast
best-effort `git pull` (6 s cap) and prints the two most recent notes plus
recent lessons for the current project.

### 3. Claude Code - or plain hooks (no plugin)

The script-based installs need a persistent checkout because their hook
commands point back to it:

```bash
git clone https://github.com/petehottelet/dekyon.git
cd dekyon

python3 install.py                    # SessionEnd only
python3 install.py --with-context     # + memory injection at SessionStart
python3 install.py --with-precompact  # + checkpoint note before compaction
python3 install.py --with-context --with-precompact  # full memory loop
python3 install.py --repo git@github.com:YOU/claude-session-notes.git
```

`install.py` merges into `~/.claude/settings.json` idempotently (re-running
never duplicates, other tools' hooks are untouched) and points at this
folder, so keep the folder somewhere stable. `python3 uninstall.py`
reverses it.

### 4. Codex CLI / desktop (optional)

From the same persistent checkout:

```bash
python3 install.py --codex
```

This idempotently merges three entries into `~/.codex/hooks.json`:

- `SessionStart` injects recent notes and lessons.
- `Stop` writes a throttled structural upsert so work survives a crash.
- `SessionEnd` replaces that same stable note with the final AI summary.

Hooks are enabled by default on current Codex builds. If they were explicitly
disabled, or `/hooks` lists nothing after installing, restore the canonical
feature flag in `~/.codex/config.toml`:

```toml
[features]
hooks = true    # codex_hooks remains a deprecated alias
```

Open `/hooks` after installation to review and trust the commands. Builds
that do not list or reliably dispatch `SessionEnd` can use the POSIX
`bin/dekyon-codex` wrapper as a fallback:

```bash
alias codex='sh "/path/to/dekyon/bin/dekyon-codex"'
```

The worker auto-detects the transcript format per file (Claude Code
transcripts vs. Codex rollouts, including `.jsonl.zst` when `zstd` or
python-zstandard is available) and recovers the session id and cwd from the
rollout when the payload omits them. Codex parsing is best-effort by
design: the rollout format still grows release to release, so unknown
record types are skipped, never fatal.

## Everyday use

You mostly do nothing - notes appear in the repo as sessions end. On
demand:

- `/dekyon:checkpoint` - save a mid-session checkpoint right now.
- "where did we leave off?", "search my session notes for the redis bug",
  "what have we learned about this project?" - the bundled skill reads the
  index, notes, and lessons ledger and answers conversationally.
- `tail -20 ~/.claude/dekyon/dekyon.log` - see what dekyon last did.

## Configuration - `~/.claude/dekyon.json`

| key | default | meaning |
| --- | --- | --- |
| `repo_dir` | `~/claude-session-notes` | local clone of the notes repo |
| `remote` / `branch` | `origin` / current | where to push |
| `push` | `true` | `false` = commit locally only |
| `summarizer` | `"claude"` | `"none"` skips AI, always structural digest |
| `model` | `"haiku"` | model for `claude -p` summarization |
| `min_user_messages` | `1` | skip sessions with fewer real typed prompts |
| `skip_reasons` | `[]` | e.g. `["clear"]` to ignore `/clear` endings |
| `max_transcript_chars` | `160000` | digest cap sent to the summarizer |
| `redact` | `true` | scrub secret-looking strings from notes |
| `lessons` | `true` | harvest `## Lessons` bullets into `lessons.md` |
| `context_lessons` | `6` | ledger lines the injector shows at start |
| `codex_stop_min_interval` | `240` | seconds between Codex Stop upserts |
| `codex_ai_upserts` | `false` | AI-summarize Codex upserts (else structural) |

Env overrides for one-offs: `DEKYON_REPO`, `DEKYON_PUSH=0`,
`DEKYON_SUMMARIZER=none`, `DEKYON_MODEL`.

## Experiencing déjà vu?

If the same problem keeps coming back, start with the log.

- **Nothing appearing?** `tail -20 ~/.claude/dekyon/dekyon.log` - every
  skip, commit, and push failure is logged with a reason.
- **Notes but no AI summaries?** The worker couldn't find or run `claude`
  (it checks PATH plus `~/.local/bin`, `/usr/local/bin`,
  `/opt/homebrew/bin`, `~/.claude/local`). Structural digests are the
  designed fallback, not an error.
- **Test without ending a session:**
  ```bash
  echo '{"session_id":"t","transcript_path":"<a .jsonl>","cwd":"'$PWD'","reason":"other"}' \
    | python3 scripts/dekyon_worker.py --stdin --dry-run
  ```
- **Windows:** native Python and git are supported. The **plugin's** hook
  commands are POSIX one-liners that Claude Code runs via Git Bash on
  Windows; without Git Bash they fall back to PowerShell and won't parse -
  use `python install.py` instead, which writes shell-free absolute-path
  hooks. The optional `bin/dekyon-codex` wrapper is POSIX-only; use
  `install.py --codex` on native Windows.

## How it works (and why)

- **Detached worker.** Hook time budgets are bounded (and the exact limits
  have shifted across Claude Code releases); summarize-then-push can take
  tens of seconds. So the hook only spools the payload and spawns the
  worker fully detached, then exits in milliseconds. Your terminal never
  waits; the worker outlives the agent.
- **Recursion guards.** `claude -p` fires hooks too, which would loop
  (session end → summarize → session end → ...). The worker sets
  `DEKYON_ACTIVE=1` (the hook bails if it's present) *and* passes
  `--settings '{"disableAllHooks": true}'` to the child claude.
- **Trivial-session filter.** Tool results ride on user-role transcript
  entries; the parser counts only real typed prompts, so opening and
  closing the agent doesn't spam the repo.
- **Concurrency and offline.** The full note/index/lessons/commit transaction
  is serialized with a file lock. Existing remote branches use
  `pull --rebase --autostash`; empty remotes are bootstrapped with the first
  push. Failures keep commits local and land in the log instead of your
  terminal.

## Contributing

Bug reports and focused pull requests are welcome. See
[CONTRIBUTING.md](CONTRIBUTING.md); report vulnerabilities privately as
described in [SECURITY.md](SECURITY.md).

## License

MIT - see [LICENSE](LICENSE).
