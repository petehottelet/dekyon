# Changelog

All notable changes to dekyon are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/); versioning is
[SemVer](https://semver.org/).

## [1.0.0] - 2026-08-17

### Added
- `install.py --codex` and the README now explain the Codex hook feature
  flag: current Codex builds ship with hooks enabled, but older builds
  gated the hook engine off by default (`[features] hooks = true`, or
  `codex_hooks = true` on the oldest builds, in `~/.codex/config.toml`).

### Changed
- Windows: the detached worker is spawned with native detach flags
  (`DETACHED_PROCESS`) instead of the POSIX-only `start_new_session`,
  so notes are still written if the host exits right after the hook fires.
- README documents that the plugin's hook commands run via Git Bash on
  Windows; PowerShell-only setups should use `install.py`, which writes
  shell-free absolute-path hooks.
- Clarified that `hooks/hooks.context.json` is the full-memory-loop hook
  set mirrored by `install.py --with-context --with-precompact`.
- Regenerated `examples/` to match real worker output (index line format,
  frontmatter quoting, auto-capture footer).
- Removed stale claims about exact Claude Code hook time budgets.

### Fixed
- AI summaries now actually run when triggered from a hook: the claude CLI
  refuses to start nested while `CLAUDECODE` is set (inherited by hooks and
  the detached worker), so the worker unsets it for the summarizer child.
  Recursion protection still comes from `DEKYON_ACTIVE` + `disableAllHooks`.
- All three entry points tolerate a UTF-8 BOM on piped JSON, which
  PowerShell adds - previously the README's manual-test command failed
  cryptically on Windows.

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
