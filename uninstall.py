#!/usr/bin/env python3
"""Remove dekyon hooks from ~/.claude/settings.json and ~/.codex/hooks.json.

Leaves the notes repo and ~/.claude/dekyon.json untouched.
"""
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

DEKYON_SCRIPTS = ("session_end.py", "session_start_context.py", "dekyon_worker.py")
DEKYON_STATUSES = {
    "Saving session note (dekyon)",
    "Loading session notes (dekyon)",
    "Saving pre-compaction note (dekyon)",
    "Checkpointing session (dekyon)",
}


def is_dekyon_handler(handler) -> bool:
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


def atomic_write_json(path: Path, doc: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, Path(str(path) + ".bak"))
    handle = tempfile.NamedTemporaryFile(
        "w", delete=False, dir=path.parent, prefix=f".{path.name}.",
        suffix=".tmp", encoding="utf-8", newline="\n",
    )
    temporary = Path(handle.name)
    try:
        with handle:
            handle.write(json.dumps(doc, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def scrub_file(path: Path, events) -> None:
    if not path.exists():
        return
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        print(f"skipping {path}: not valid JSON")
        return
    if not isinstance(doc, dict) or not isinstance(doc.get("hooks", {}), dict):
        print(f"skipping {path}: expected a JSON object with an object-valued hooks key")
        return
    hooks = doc.get("hooks", {})
    touched = False
    for event in events:
        kept = []
        groups = hooks.get(event, [])
        if not isinstance(groups, list):
            continue
        event_touched = False
        for group in groups:
            if not isinstance(group, dict):
                kept.append(group)
                continue
            existing = group.get("hooks", [])
            if not isinstance(existing, list):
                kept.append(group)
                continue
            hs = [handler for handler in existing if not is_dekyon_handler(handler)]
            if len(hs) == len(existing):
                kept.append(group)
                continue
            event_touched = True
            if len(hs) != len(existing):
                touched = True
            if hs:
                kept.append(dict(group, hooks=hs))
        if event_touched:
            if kept:
                hooks[event] = kept
            else:
                hooks.pop(event, None)
    if not hooks:
        doc.pop("hooks", None)
    if touched:
        atomic_write_json(path, doc)
        print(f"dekyon hooks removed from {path}")


def main() -> int:
    events = ("SessionEnd", "SessionStart", "PreCompact", "Stop")
    scrub_file(Path.home() / ".claude" / "settings.json", events)
    scrub_file(Path.home() / ".codex" / "hooks.json", events)
    return 0


if __name__ == "__main__":
    sys.exit(main())
