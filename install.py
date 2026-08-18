#!/usr/bin/env python3
"""Wire dekyon hooks into ~/.claude/settings.json (idempotent).

For people who prefer plain hooks over the plugin drop-in. Copies nothing:
hooks point at this folder, so keep it somewhere stable (e.g. ~/tools/).

Usage:
    python3 install.py                          # SessionEnd hook only
    python3 install.py --with-context           # + SessionStart memory injection
    python3 install.py --with-precompact        # + checkpoint note before compaction
    python3 install.py --codex                  # also wire ~/.codex/hooks.json
    python3 install.py --repo git@github.com:YOU/claude-session-notes.git
    python3 install.py --repo ~/somewhere/notes # existing local path works too
"""
import argparse
import json
import shlex
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SETTINGS = Path.home() / ".claude" / "settings.json"
CONFIG = Path.home() / ".claude" / "dekyon.json"
MARK = "dekyon"


def our(handler: dict) -> bool:
    return MARK in json.dumps(handler)


def scrub(groups):
    kept = []
    for g in groups or []:
        hooks = [h for h in g.get("hooks", []) if not our(h)]
        if hooks:
            g = dict(g, hooks=hooks)
            kept.append(g)
    return kept


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", help="GitHub URL to clone, or existing local path, for the notes repo")
    ap.add_argument("--with-context", action="store_true",
                    help="also inject recent notes at SessionStart")
    ap.add_argument("--with-precompact", action="store_true",
                    help="also save a checkpoint note before context compaction")
    ap.add_argument("--codex", action="store_true",
                    help="also wire Codex CLI hooks into ~/.codex/hooks.json")
    args = ap.parse_args()

    settings = {}
    if SETTINGS.exists():
        try:
            settings = json.loads(SETTINGS.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            sys.exit(f"{SETTINGS} is not valid JSON; fix it first (no changes made).")

    hooks = settings.setdefault("hooks", {})
    end_hook = {"type": "command", "command": sys.executable,
                "args": [str(HERE / "scripts" / "session_end.py")], "timeout": 15,
                "statusMessage": "Saving session note (dekyon)"}
    hooks["SessionEnd"] = scrub(hooks.get("SessionEnd")) + [{"hooks": [end_hook]}]

    hooks["SessionStart"] = scrub(hooks.get("SessionStart"))
    if args.with_context:
        start_hook = {"type": "command", "command": sys.executable,
                      "args": [str(HERE / "scripts" / "session_start_context.py")], "timeout": 10,
                      "statusMessage": "Loading session notes (dekyon)"}
        hooks["SessionStart"].append({"matcher": "startup|resume|clear", "hooks": [start_hook]})
    if not hooks["SessionStart"]:
        del hooks["SessionStart"]

    hooks["PreCompact"] = scrub(hooks.get("PreCompact"))
    if args.with_precompact:
        pc_hook = {"type": "command", "command": sys.executable,
                   "args": [str(HERE / "scripts" / "session_end.py")], "timeout": 15,
                   "statusMessage": "Saving pre-compaction note (dekyon)"}
        hooks["PreCompact"].append({"matcher": "manual|auto", "hooks": [pc_hook]})
    if not hooks["PreCompact"]:
        del hooks["PreCompact"]

    SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    print(f"hooks written to {SETTINGS}")

    if args.codex:
        codex_hooks_path = Path.home() / ".codex" / "hooks.json"
        doc = {}
        if codex_hooks_path.exists():
            try:
                doc = json.loads(codex_hooks_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                sys.exit(f"{codex_hooks_path} is not valid JSON; fix it first "
                         "(Claude-side changes above were applied).")
        chooks = doc.setdefault("hooks", {})
        # Codex user hooks use command strings, so write absolute paths plus a
        # Windows-specific override generated from the interpreter running this
        # installer. This avoids assuming that `python3` is on PATH.
        start_argv = [sys.executable, str(HERE / "scripts" / "session_start_context.py")]
        end_argv = [sys.executable, str(HERE / "scripts" / "session_end.py")]
        stop_argv = end_argv + ["--upsert"]

        def codex_command(argv):
            return {
                "command": " ".join(shlex.quote(part) for part in argv),
                "commandWindows": subprocess.list2cmdline(argv),
            }

        chooks["SessionStart"] = scrub(chooks.get("SessionStart")) + [{"hooks": [
            {"type": "command", **codex_command(start_argv), "timeout": 10,
             "statusMessage": "Loading session notes (dekyon)"}]}]
        chooks["SessionEnd"] = scrub(chooks.get("SessionEnd")) + [{"hooks": [
            {"type": "command", **codex_command(end_argv), "timeout": 3,
             "statusMessage": "Saving session note (dekyon)"}]}]
        chooks["Stop"] = scrub(chooks.get("Stop")) + [{"hooks": [
            {"type": "command", **codex_command(stop_argv), "timeout": 20,
             "statusMessage": "Checkpointing session (dekyon)"}]}]
        codex_hooks_path.parent.mkdir(parents=True, exist_ok=True)
        codex_hooks_path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
        print(f"Codex hooks written to {codex_hooks_path}")
        print("  SessionStart restores context, Stop writes crash-tolerant upserts,\n"
              "  and SessionEnd replaces the upsert with the final note. Review and\n"
              "  trust the new commands in Codex with /hooks.\n"
              "  If /hooks lists nothing, your Codex build gates the hook engine\n"
              "  behind a feature flag; add to ~/.codex/config.toml:\n"
              "    [features]\n"
              "    hooks = true    # the oldest builds used: codex_hooks = true")

    if args.repo:
        cfg = {}
        if CONFIG.exists():
            try:
                cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                cfg = {}
        if "://" in args.repo or args.repo.startswith("git@"):
            dest = Path.home() / "claude-session-notes"
            if not dest.exists():
                rc = subprocess.run(["git", "clone", args.repo, str(dest)]).returncode
                if rc != 0:
                    print("clone failed; init'ing locally and adding the remote instead")
                    subprocess.run(["git", "init", "-b", "main", str(dest)])
                    subprocess.run(["git", "-C", str(dest), "remote", "add", "origin", args.repo])
            cfg["repo_dir"] = str(dest)
        else:
            cfg["repo_dir"] = str(Path(args.repo).expanduser().resolve())
        CONFIG.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
        print(f"notes repo set to {cfg['repo_dir']} in {CONFIG}")

    print("done. Restart Claude Code (or start a new session) and check /hooks.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
