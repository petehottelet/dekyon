#!/usr/bin/env python3
"""dekyon . SessionEnd launcher.

Claude Code gives hooks a bounded time budget (the exact limits have
shifted across releases). An AI-written session note plus a git push can
take tens of seconds, so this launcher does the minimum possible work:

  1. read the hook payload from stdin
  2. spool it to disk
  3. spawn dekyon_worker.py fully detached (new session, no inherited pipes)
  4. exit 0

The detached worker survives Claude Code's exit and does the slow parts
(transcript parsing, summarization, git commit/push) on its own time.
"""
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

STATE_DIR = Path(os.environ.get("DEKYON_STATE", Path.home() / ".claude" / "dekyon"))
LOG_FILE = STATE_DIR / "dekyon.log"


def log(msg: str) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} [launcher] {msg}\n")
    except OSError:
        pass


def main() -> int:
    # Recursion guard: the worker may invoke `claude -p` for the summary,
    # and headless runs fire hooks too. If we're inside our own child, bail.
    if os.environ.get("DEKYON_ACTIVE"):
        return 0

    try:
        # utf-8-sig tolerates the BOM PowerShell adds when piping on Windows.
        raw = (sys.stdin.buffer.read().decode("utf-8-sig")
               if hasattr(sys.stdin, "buffer") else sys.stdin.read())
        payload = json.loads(raw.lstrip("\ufeff"))
    except (json.JSONDecodeError, ValueError) as e:
        log(f"could not parse hook payload: {e}")
        return 0  # never make noise at session end

    upsert = "--upsert" in sys.argv[1:]
    accepted = {None, "SessionEnd", "PreCompact"}
    if upsert:  # Codex Stop drives crash-tolerant throttled upserts
        accepted |= {"Stop"}
    if payload.get("hook_event_name") not in accepted:
        return 0
    if upsert:
        # Codex Stop hooks expect JSON on stdout when the command succeeds.
        # The real work is detached, so there is no steering decision to add.
        print("{}")

    spool = STATE_DIR / "spool"
    try:
        spool.mkdir(parents=True, exist_ok=True)
        sid = str(payload.get("session_id") or uuid.uuid4())[:32]
        payload_path = spool / f"{sid}-{uuid.uuid4().hex[:6]}.json"
        payload_path.write_text(json.dumps(payload), encoding="utf-8")
    except OSError as e:
        log(f"could not spool payload: {e}")
        return 0

    worker = Path(__file__).resolve().parent / "dekyon_worker.py"
    env = dict(os.environ, DEKYON_ACTIVE="1")
    try:
        logf = open(LOG_FILE, "a")  # worker inherits this for stray stderr
    except OSError:
        logf = subprocess.DEVNULL

    # Detach so the worker survives the host's exit: setsid on POSIX,
    # DETACHED_PROCESS on Windows (start_new_session is a no-op there and
    # the host may tear down its process tree when it quits).
    detach = {}
    if os.name == "nt":
        detach["creationflags"] = (subprocess.DETACHED_PROCESS
                                   | subprocess.CREATE_NEW_PROCESS_GROUP)
    else:
        detach["start_new_session"] = True
    try:
        subprocess.Popen(
            [sys.executable, str(worker), str(payload_path)]
            + (["--upsert"] if upsert else []),
            stdin=subprocess.DEVNULL,
            stdout=logf,
            stderr=logf,
            env=env,
            cwd=str(Path.home()),
            **detach,
        )
        log(f"detached worker for session {payload.get('session_id')} (reason={payload.get('reason')})")
    except OSError as e:
        log(f"could not start worker: {e}")
    finally:
        if logf != subprocess.DEVNULL:
            logf.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
