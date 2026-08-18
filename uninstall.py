#!/usr/bin/env python3
"""Remove dekyon hooks from ~/.claude/settings.json and ~/.codex/hooks.json.

Leaves the notes repo and ~/.claude/dekyon.json untouched.
"""
import json
from pathlib import Path


def scrub_file(path: Path, events) -> None:
    if not path.exists():
        return
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        print(f"skipping {path}: not valid JSON")
        return
    hooks = doc.get("hooks", {})
    touched = False
    for event in events:
        kept = []
        for group in hooks.get(event, []):
            hs = [h for h in group.get("hooks", []) if "dekyon" not in json.dumps(h)]
            if len(hs) != len(group.get("hooks", [])):
                touched = True
            if hs:
                kept.append(dict(group, hooks=hs))
        if hooks.get(event) != kept:
            touched = True
        if kept:
            hooks[event] = kept
        else:
            hooks.pop(event, None)
    if not hooks:
        doc.pop("hooks", None)
    if touched:
        path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
        print(f"dekyon hooks removed from {path}")


scrub_file(Path.home() / ".claude" / "settings.json",
           ("SessionEnd", "SessionStart", "PreCompact", "Stop"))
scrub_file(Path.home() / ".codex" / "hooks.json",
           ("SessionEnd", "SessionStart", "PreCompact", "Stop"))
