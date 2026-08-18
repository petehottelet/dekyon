# Changelog

All notable changes to dekyon are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/); versioning is
[SemVer](https://semver.org/).

## [0.5.0] - 2026-08-18

### Added
- Integration coverage for empty-remote bootstrapping, path-isolated commits,
  concurrent captures, bounded transcript digests, Codex rollout matching,
  configuration atomicity, and failed startup rebases.
- A scheduled ecosystem-compatibility workflow tests the current Claude Code
  and skills CLI releases while pull requests use reproducible pinned checks.
- GitHub Actions use the current Node 24-based checkout and setup actions,
  avoiding deprecated action-runtime warnings.
- `install.py --codex` and the README now explain the Codex hook feature
  flag: hooks are enabled by default, `hooks` is the canonical key, and
  `codex_hooks` remains a deprecated alias.

### Changed
- Windows: the detached worker is spawned with native detach flags
  (`DETACHED_PROCESS`) instead of the POSIX-only `start_new_session`,
  so notes are still written if the host exits right after the hook fires.
- README documents that the plugin's hook commands run via Git Bash on
  Windows; PowerShell-only setups should use `install.py`, which writes
  shell-free absolute-path hooks.
- Clarified that `hooks/hooks.context.json` is the full-memory-loop hook
  set mirrored by `install.py --with-context --with-precompact`.
- Transcript parsing now streams plain and zstd JSONL into a bounded head/tail
  digest instead of loading the entire rollout into memory.
- The bundled skill offers proactive checkpoints but requires acceptance
  before writing, committing, or pushing them; checkpoint filenames include
  seconds to prevent same-minute collisions.
- `uninstall.py` no longer runs at import time (proper `main()` guard),
  making it safely importable and unit-testable.
- Regenerated `examples/` to match real worker output (index line format,
  frontmatter quoting, auto-capture footer).
- Removed stale claims about exact Claude Code hook time budgets.

### Fixed
- The first note now bootstraps an empty remote branch instead of failing its
  pre-push pull forever.
- Automated commits use an explicit pathspec, preserve unrelated staged work,
  and stop safely when `git add` fails.
- The note, index, lessons ledger, commit, and push now share one lock, closing
  races that could lose index entries or combine simultaneous sessions.
- The nested AI summarizer treats the digest as untrusted and disables built-in
  tools, MCP tools, skills, hooks, and session persistence.
- SessionStart pulls only the configured remote and aborts a rebase that its
  own failed or timed-out refresh started.
- Install and uninstall configuration writes are backed up and atomically
  replaced. All target JSON is validated before any install-side mutation,
  and hook removal uses exact Dekyon signatures instead of a substring test.
- Codex command extraction recognizes the current `cmd` argument, Stop
  throttling uses recovered session IDs, and rollout fallback refuses a known
  session mismatch rather than capturing another concurrent conversation.
- Markdown link labels are escaped before writing index and lessons entries.
- AI summaries now actually run when triggered from a hook: the claude CLI
  refuses to start nested while `CLAUDECODE` is set (inherited by hooks and
  the detached worker), so the worker unsets it for the summarizer child.
  Recursion protection still comes from `DEKYON_ACTIVE` + `disableAllHooks`.
- All three entry points tolerate a UTF-8 BOM on piped JSON, which
  PowerShell adds - previously the README's manual-test command failed
  cryptically on Windows.
- Windows encoding: subprocess I/O (summarizer stdin/stdout, git output,
  zstd decompression) is pinned to UTF-8 instead of the locale codec
  (cp1252), which crashed note-writing on digests containing check marks,
  emoji, or other non-cp1252 characters; hook stdout is reconfigured to
  UTF-8 so context injection of such notes doesn't silently fail either.

## [0.2.0] - 2026-08-17

### Added
- **Codex support.** `install.py --codex` wires SessionStart, Stop, and
  SessionEnd hooks into `~/.codex/hooks.json`: Stop provides crash-tolerant
  upserts and SessionEnd replaces the same stable note with the final
  summary. A POSIX exit wrapper remains available for older or non-interactive
  builds. Rollout format (including `.jsonl.zst`) is auto-detected.
- **Pre-compaction checkpoints.** `install.py --with-precompact` (or the
  bundled context hooks) writes a `kind: precompact` note before Claude Code
  compacts, so detail isn't lost to compaction.
- **Lessons ledger.** Notes now include a `## Lessons` section; durable,
  reusable bullets are appended to `sessions/<project>/lessons.md` and the
  last few are injected at session start.
- **Noise-tool filter.** Bookkeeping tools (TodoWrite, SlashCommand, etc.)
  no longer count as activity in notes or stats.
- `marketplace.json` for `/plugin marketplace add` distribution.

### Changed
- Renamed the project to **dekyon** (config `~/.claude/dekyon.json`, state
  `~/.claude/dekyon/`, env `DEKYON_*`, command `/dekyon:checkpoint`).
- Skill rewritten to Anthropic skill-authoring guidance (pushier triggers,
  exact note template, worked recall example).
- Added cross-platform file locking, Windows-aware hook commands, safe YAML
  frontmatter quoting, deterministic tests, and GitHub Actions CI.

## [0.1.0] - 2026-08-17

### Added
- Initial release: SessionEnd hook + detached worker that summarizes a
  Claude Code session into a markdown note and commits + pushes it to a
  git repo. Optional SessionStart context injection. Bundled recall skill
  and `/checkpoint` command.
