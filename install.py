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
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SETTINGS = Path.home() / ".claude" / "settings.json"
CONFIG = Path.home() / ".claude" / "dekyon.json"
DEKYON_SCRIPTS = ("session_end.py", "session_start_context.py", "dekyon_worker.py")
DEKYON_STATUSES = {
    "Saving session note (dekyon)",
    "Loading session notes (dekyon)",
    "Saving pre-compaction note (dekyon)",
    "Checkpointing session (dekyon)",
}


def our(handler: dict) -> bool:
    if not isinstance(handler, dict):
        return False
    values = [str(handler.get("command", ""))]
    if isinstance(handler.get("args"), list):
        values.extend(str(value) for value in handler["args"])
    command = " ".join(values).replace("\\", "/").lower()
    script_match = any(script in command for script in DEKYON_SCRIPTS)
    branded = str(handler.get("statusMessage", "")) in DEKYON_STATUSES
    legacy_path = "/dekyon/scripts/" in command
    plugin_path = "${claude_plugin_root}/scripts/" in command
    return script_match and (branded or legacy_path or plugin_path)


def scrub(groups):
    kept = []
    for g in groups if isinstance(groups, list) else []:
        if not isinstance(g, dict):
            kept.append(g)
            continue
        existing = g.get("hooks", [])
        if not isinstance(existing, list):
            kept.append(g)
            continue
        hooks = [h for h in existing if not our(h)]
        if len(hooks) == len(existing):
            kept.append(g)
        elif hooks:
            kept.append(dict(g, hooks=hooks))
    return kept


def read_json_object(path: Path):
    if not path.exists():
        return {}, None
    try:
        original = path.read_text(encoding="utf-8")
        doc = json.loads(original)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(doc, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return doc, original


def _write_temp_text(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w", delete=False, dir=path.parent, prefix=f".{path.name}.",
        suffix=".tmp", encoding="utf-8", newline="\n",
    )
    try:
        with handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        return Path(handle.name)
    except Exception:
        Path(handle.name).unlink(missing_ok=True)
        raise


def _write_temp_json(path: Path, doc: dict) -> Path:
    return _write_temp_text(path, json.dumps(doc, indent=2) + "\n")


def write_json_documents(documents) -> None:
    """Validate, back up, and atomically replace a set of JSON documents."""
    prepared = []
    try:
        for path, doc, original in documents:
            current = path.read_text(encoding="utf-8") if path.exists() else None
            if current != original:
                raise RuntimeError(f"{path} changed while dekyon was installing; retry")
            prepared.append((path, _write_temp_json(path, doc), original))

        for path, _, original in prepared:
            current = path.read_text(encoding="utf-8") if path.exists() else None
            if current != original:
                raise RuntimeError(f"{path} changed while dekyon was installing; retry")

        for path, _, original in prepared:
            if original is not None:
                shutil.copy2(path, Path(str(path) + ".bak"))

        replaced = []
        try:
            for path, temporary, original in prepared:
                os.replace(temporary, path)
                replaced.append((path, original))
        except OSError:
            for path, original in reversed(replaced):
                if original is None:
                    path.unlink(missing_ok=True)
                else:
                    rollback = _write_temp_text(path, original)
                    os.replace(rollback, path)
            raise
    finally:
        for _, temporary, _ in prepared:
            temporary.unlink(missing_ok=True)


def normalized_remote(url: str) -> str:
    value = str(url).strip().rstrip("/")
    return value[:-4] if value.lower().endswith(".git") else value


def hooks_object(doc: dict, path: Path) -> dict:
    hooks = doc.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError(f"{path} has a non-object 'hooks' value")
    return hooks


def resolve_notes_repo(value: str) -> Path:
    if "://" not in value and not value.startswith("git@"):
        return Path(value).expanduser().resolve()

    dest = Path.home() / "claude-session-notes"
    if not dest.exists():
        result = subprocess.run(["git", "clone", value, str(dest)])
        if result.returncode != 0:
            raise RuntimeError("git clone failed; hook settings were not changed")
    if not (dest / ".git").exists():
        raise RuntimeError(f"{dest} exists but is not a git repository")
    current = subprocess.run(
        ["git", "-C", str(dest), "remote", "get-url", "origin"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if current.returncode == 0:
        if normalized_remote(current.stdout) != normalized_remote(value):
            raise RuntimeError(
                f"{dest} already uses a different origin ({current.stdout.strip()}); "
                "choose a local path or fix that remote explicitly"
            )
    else:
        added = subprocess.run(
            ["git", "-C", str(dest), "remote", "add", "origin", value]
        )
        if added.returncode != 0:
            raise RuntimeError(f"could not add origin to {dest}")
    return dest


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

    codex_hooks_path = Path.home() / ".codex" / "hooks.json"
    try:
        settings, settings_original = read_json_object(SETTINGS)
        if args.codex:
            codex_doc, codex_original = read_json_object(codex_hooks_path)
        else:
            codex_doc, codex_original = None, None
        if args.repo:
            cfg, config_original = read_json_object(CONFIG)
        else:
            cfg, config_original = None, None
    except ValueError as exc:
        sys.exit(f"{exc} (no changes made).")

    try:
        hooks = hooks_object(settings, SETTINGS)
    except ValueError as exc:
        sys.exit(f"{exc} (no changes made).")
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

    if args.codex:
        try:
            chooks = hooks_object(codex_doc, codex_hooks_path)
        except ValueError as exc:
            sys.exit(f"{exc} (no changes made).")
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
             "statusMessage": "Loading session notes (dekyon)",
             "additionalContextLimit": 1500}]}]
        chooks["SessionEnd"] = scrub(chooks.get("SessionEnd")) + [{"hooks": [
            {"type": "command", **codex_command(end_argv), "timeout": 3,
             "statusMessage": "Saving session note (dekyon)"}]}]
        chooks["Stop"] = scrub(chooks.get("Stop")) + [{"hooks": [
            {"type": "command", **codex_command(stop_argv), "timeout": 20,
             "statusMessage": "Checkpointing session (dekyon)"}]}]
    if args.repo:
        try:
            cfg["repo_dir"] = str(resolve_notes_repo(args.repo))
        except RuntimeError as exc:
            sys.exit(f"{exc} (hook settings were not changed).")

    documents = [(SETTINGS, settings, settings_original)]
    if args.codex:
        documents.append((codex_hooks_path, codex_doc, codex_original))
    if args.repo:
        documents.append((CONFIG, cfg, config_original))
    try:
        write_json_documents(documents)
    except (OSError, RuntimeError, ValueError) as exc:
        sys.exit(f"install failed safely: {exc}")

    print(f"hooks written to {SETTINGS}")
    if args.codex:
        print(f"Codex hooks written to {codex_hooks_path}")
        print("  SessionStart restores context, Stop writes crash-tolerant upserts,\n"
              "  and SessionEnd replaces the upsert with the final note. Review and\n"
              "  trust the new commands in Codex with /hooks.\n"
              "  Hooks are enabled by default. If they were disabled explicitly,\n"
              "  restore them in ~/.codex/config.toml with:\n"
              "    [features]\n"
              "    hooks = true    # codex_hooks remains a deprecated alias")
    if args.repo:
        print(f"notes repo set to {cfg['repo_dir']} in {CONFIG}")

    print("done. Restart Claude Code (or start a new session) and check /hooks.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
