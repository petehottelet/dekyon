#!/usr/bin/env python3
"""dekyon . detached worker.

Turns a finished session into a narrative markdown note and commits it to a
local git repo that syncs with GitHub over your normal git credentials. Runs
after the host has already exited, so it can take its time; every failure is
logged, never surfaced. Reads both Claude Code transcripts and Codex CLI
rollout files (format is auto-detected per file).

Usage:
  dekyon_worker.py <payload.json> [--dry-run] [--upsert]
  dekyon_worker.py --stdin [--dry-run] [--upsert]   (payload JSON on stdin)

Payload = the hook input: session_id, transcript_path, cwd, reason, and for
PreCompact events a trigger. --upsert (used by the Codex Stop hook) rewrites
one stable note per session instead of creating a new file, throttled by
`codex_stop_min_interval` so per-turn Stop events don't spam the repo.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

try:  # POSIX advisory file locking
    import fcntl  # type: ignore
except ImportError:  # pragma: no cover - exercised on Windows
    fcntl = None

try:  # Windows advisory file locking
    import msvcrt  # type: ignore
except ImportError:  # pragma: no cover - exercised on POSIX
    msvcrt = None

STATE_DIR = Path(os.environ.get("DEKYON_STATE", Path.home() / ".claude" / "dekyon"))
CONFIG_PATH = Path(os.environ.get("DEKYON_CONFIG", Path.home() / ".claude" / "dekyon.json"))
LOG_FILE = STATE_DIR / "dekyon.log"

DEFAULTS = {
    "repo_dir": str(Path.home() / "claude-session-notes"),
    "remote": "origin",
    "branch": "",              # empty = repo's current branch
    "push": True,
    "summarizer": "claude",    # "claude" | "none"
    "model": "haiku",
    "min_user_messages": 1,
    "skip_reasons": [],         # e.g. ["clear"] to ignore /clear
    "max_transcript_chars": 160000,
    "redact": True,
    "lessons": True,               # append '## Lessons' bullets to a per-project ledger
    "context_lessons": 6,          # how many ledger lines the SessionStart injector shows
    "codex_stop_min_interval": 240,  # seconds between Codex Stop-hook upserts
    "codex_ai_upserts": False,     # upserts use the structural digest unless enabled
}

EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}

# Meta/bookkeeping tools that add noise, not signal (pattern from claude-mem's
# observation blocklist): skip them in stats and in the digest.
SKIP_TOOLS = {"TodoWrite", "SlashCommand", "Skill", "AskUserQuestion",
              "ListMcpResourcesTool", "TodoRead"}

REDACT_PATTERNS = [
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),                      # GitHub tokens
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"sk-(?:ant-)?[A-Za-z0-9_\-]{20,}"),                # OpenAI/Anthropic-style keys
    re.compile(r"xox[baprs]-[A-Za-z0-9\-]{10,}"),                  # Slack
    re.compile(r"AKIA[0-9A-Z]{16}"),                                # AWS access key id
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)\b(api[_-]?key|secret|token|password|passwd)\b(\s*[:=]\s*)(['\"]?)[^\s'\"]{8,}\3"),
]


# ---------------------------------------------------------------- utilities

def log(msg: str) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} [worker] {msg}\n")
        if LOG_FILE.stat().st_size > 1_000_000:
            LOG_FILE.replace(LOG_FILE.with_suffix(".log.old"))
    except OSError:
        pass


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        try:
            user_cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            cfg.update({k: v for k, v in user_cfg.items() if not k.startswith("_")})
        except (json.JSONDecodeError, OSError) as e:
            log(f"bad config at {CONFIG_PATH}: {e}; using defaults")
    else:
        try:
            CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
            doc = {"_readme": "dekyon config. repo_dir is a local git clone that syncs "
                              "to GitHub; summarizer 'claude' uses `claude -p` (falls back to a "
                              "structural digest), 'none' skips AI entirely.", **DEFAULTS}
            CONFIG_PATH.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
            log(f"wrote default config to {CONFIG_PATH}")
        except OSError:
            pass
    # env overrides
    if os.environ.get("DEKYON_REPO"):
        cfg["repo_dir"] = os.environ["DEKYON_REPO"]
    if os.environ.get("DEKYON_SUMMARIZER"):
        cfg["summarizer"] = os.environ["DEKYON_SUMMARIZER"]
    if os.environ.get("DEKYON_MODEL"):
        cfg["model"] = os.environ["DEKYON_MODEL"]
    if os.environ.get("DEKYON_PUSH") in ("0", "false", "no"):
        cfg["push"] = False
    return cfg


def redact(text: str) -> str:
    for pat in REDACT_PATTERNS:
        text = pat.sub("[REDACTED]", text)
    return text


def slugify(text: str, max_len: int = 40) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:max_len].rstrip("-") or "session"


def parse_ts(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def run(cmd, cwd=None, timeout=30, env=None):
    """Run a command; return (rc, stdout, stderr) without ever raising."""
    try:
        p = subprocess.run(cmd, cwd=cwd, timeout=timeout, env=env,
                           capture_output=True, text=True)
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except (subprocess.TimeoutExpired, OSError) as e:
        return 124, "", str(e)


@contextmanager
def git_lock(path: Path):
    """Best-effort cross-platform lock for serializing notes-repo writes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as lock:
        locked = False
        try:
            if fcntl is not None:
                fcntl.flock(lock, fcntl.LOCK_EX)
                locked = True
            elif msvcrt is not None:
                lock.seek(0, os.SEEK_END)
                if lock.tell() == 0:
                    lock.write(b"\0")
                    lock.flush()
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
                locked = True
        except OSError:
            pass
        try:
            yield
        finally:
            if locked:
                try:
                    if fcntl is not None:
                        fcntl.flock(lock, fcntl.LOCK_UN)
                    elif msvcrt is not None:
                        lock.seek(0)
                        msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass


# ---------------------------------------------------------- transcript parse

def parse_transcript(path: Path, max_chars: int) -> dict:
    """Walk the JSONL transcript into stats + a plain-text conversation digest.

    Only real user prompts count as user messages; tool_result payloads come
    back on user-role entries and must not inflate the count or the digest.
    """
    turns = []              # (role, text) with tool one-liners inline
    user_msgs = assistant_msgs = 0
    tool_counts = {}
    files_touched = []
    bash_cmds = []
    first_prompt = ""
    model = ""
    branch = ""
    first_ts = last_ts = None

    text = read_transcript_text(path)
    if text is None:
        return {}
    lines = text.splitlines()
    if looks_like_codex(lines):
        return parse_codex_rollout(lines, max_chars)

    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        etype = entry.get("type")
        if etype not in ("user", "assistant") or entry.get("isMeta"):
            continue
        ts = parse_ts(entry.get("timestamp"))
        if ts:
            first_ts = first_ts or ts
            last_ts = ts
        branch = entry.get("gitBranch") or branch
        msg = entry.get("message") or {}
        content = msg.get("content")

        if etype == "user":
            text_parts = []
            if isinstance(content, str):
                text_parts.append(content)
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text_parts.append(block.get("text", ""))
            text = "\n".join(t for t in text_parts if t).strip()
            if not text:
                continue  # tool_result-only entry
            if text.startswith(("<command-name>", "<local-command")):
                continue  # slash-command bookkeeping
            user_msgs += 1
            first_prompt = first_prompt or text
            turns.append(("USER", text))
        else:
            model = msg.get("model") or model
            assistant_msgs += 1
            for block in content if isinstance(content, list) else []:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text" and block.get("text", "").strip():
                    turns.append(("ASSISTANT", block["text"].strip()))
                elif btype == "tool_use":
                    name = block.get("name", "tool")
                    if name in SKIP_TOOLS:
                        continue
                    tool_counts[name] = tool_counts.get(name, 0) + 1
                    inp = block.get("input") or {}
                    detail = ""
                    fp = inp.get("file_path") or inp.get("notebook_path")
                    if name in EDIT_TOOLS and fp:
                        if fp not in files_touched:
                            files_touched.append(fp)
                        detail = fp
                    elif name == "Bash" and inp.get("command"):
                        cmd = " ".join(str(inp["command"]).split())[:100]
                        if len(bash_cmds) < 12 and cmd not in bash_cmds:
                            bash_cmds.append(cmd)
                        detail = cmd
                    elif fp:
                        detail = fp
                    turns.append(("ASSISTANT", f"[tool: {name}{' ' + detail if detail else ''}]"))

    # Assemble digest; favor the end of the session when truncating.
    parts = []
    for role, text in turns:
        if len(text) > 2000:
            text = text[:2000] + " ...[truncated]"
        parts.append(f"{role}: {text}")
    digest = "\n\n".join(parts)
    if len(digest) > max_chars:
        head = digest[: max_chars // 4]
        tail = digest[-(max_chars * 3 // 4):]
        digest = head + "\n\n...[middle of session omitted]...\n\n" + tail

    duration_min = None
    if first_ts and last_ts and last_ts > first_ts:
        duration_min = round((last_ts - first_ts).total_seconds() / 60)

    return {
        "digest": digest,
        "user_msgs": user_msgs,
        "assistant_msgs": assistant_msgs,
        "tool_counts": tool_counts,
        "files_touched": files_touched,
        "bash_cmds": bash_cmds,
        "first_prompt": first_prompt,
        "model": model,
        "branch": branch,
        "duration_min": duration_min,
        "started_at": first_ts.isoformat() if first_ts else "",
    }


def read_transcript_text(path: Path):
    """Read a transcript, transparently handling Codex's .jsonl.zst files."""
    try:
        if path.suffix == ".zst":
            rc, out, _ = run(["zstd", "-dc", str(path)], timeout=60)
            if rc == 0 and out:
                return out
            try:
                import zstandard  # type: ignore
                return zstandard.ZstdDecompressor().decompress(
                    path.read_bytes()).decode("utf-8", errors="replace")
            except Exception as e:
                log(f"cannot decompress {path}: {e} (install zstd or python-zstandard)")
                return None
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        log(f"cannot read transcript {path}: {e}")
        return None


CODEX_LINE_TYPES = {"session_meta", "response_item", "event_msg",
                    "turn_context", "compacted"}


def looks_like_codex(lines) -> bool:
    """Sniff the first few parseable lines: Codex rollouts wrap everything in
    {timestamp, type, payload}; Claude transcripts carry a `message` key."""
    for line in lines[:8]:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("type") in CODEX_LINE_TYPES:
            return True
        if entry.get("type") in ("user", "assistant") and "message" in entry:
            return False
    return False


CODEX_INJECTED_PREFIXES = ("<user_instructions>", "<environment_context>",
                           "<ide_context>", "<permissions", "# AGENTS")


def parse_codex_rollout(lines, max_chars: int) -> dict:
    """Codex CLI rollout: RolloutLine {timestamp, type, payload}. Conversation
    lives in response_item payloads (message / function_call / ...); the first
    line is session_meta. Best-effort by design: the record set still grows
    release to release, so unknown types are skipped, never fatal."""
    turns = []
    user_msgs = assistant_msgs = 0
    tool_counts = {}
    files_touched = []
    bash_cmds = []
    first_prompt = ""
    model = ""
    meta_sid = meta_cwd = ""
    first_ts = last_ts = None

    def note_cmd(cmd):
        cmd = " ".join(str(cmd).split())[:100]
        if cmd and len(bash_cmds) < 12 and cmd not in bash_cmds:
            bash_cmds.append(cmd)
        return cmd

    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        ts = parse_ts(entry.get("timestamp"))
        if ts:
            first_ts = first_ts or ts
            last_ts = ts
        etype = entry.get("type")
        p = entry.get("payload") or {}
        if not isinstance(p, dict):
            continue

        if etype == "session_meta":
            inner = p.get("meta") if isinstance(p.get("meta"), dict) else p
            meta_sid = str(inner.get("id") or inner.get("session_id") or meta_sid)
            meta_cwd = inner.get("cwd") or meta_cwd
        elif etype == "turn_context":
            meta_cwd = p.get("cwd") or meta_cwd
            m = p.get("model")
            model = (m if isinstance(m, str) else (m or {}).get("model", "")) or model
        elif etype == "response_item":
            itype = p.get("type")
            if itype == "message":
                role = p.get("role", "")
                parts = [c.get("text", "") for c in p.get("content") or []
                         if isinstance(c, dict) and c.get("text")]
                text = "\n".join(t for t in parts if t).strip()
                if not text:
                    continue
                if role == "user":
                    if text.lstrip().startswith(CODEX_INJECTED_PREFIXES):
                        continue  # AGENTS.md / env context injected as user role
                    user_msgs += 1
                    first_prompt = first_prompt or text
                    turns.append(("USER", text))
                elif role == "assistant":
                    assistant_msgs += 1
                    turns.append(("ASSISTANT", text))
            elif itype in ("function_call", "local_shell_call", "custom_tool_call"):
                name = p.get("name") or itype
                tool_counts[name] = tool_counts.get(name, 0) + 1
                raw_args = p.get("arguments") or p.get("input") or ""
                try:
                    a = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
                except (json.JSONDecodeError, TypeError, ValueError):
                    a = {}
                detail = ""
                cmd = a.get("command") or (p.get("action") or {}).get("command")
                if isinstance(cmd, list):
                    cmd = " ".join(str(x) for x in cmd)
                if cmd:
                    detail = note_cmd(cmd)
                blob = str(raw_args)
                for m in re.finditer(r"\*\*\* (?:Update|Add|Delete) File: (.+)", blob):
                    fp = m.group(1).strip()
                    if fp and fp not in files_touched:
                        files_touched.append(fp)
                        detail = detail or fp
                fp = a.get("file_path") or a.get("path")
                if fp and fp not in files_touched:
                    files_touched.append(fp)
                    detail = detail or fp
                turns.append(("ASSISTANT", f"[tool: {name}{' ' + detail if detail else ''}]"))
            # reasoning / function_call_output / web_search_call etc: skip
        # event_msg, compacted, world_state, ...: skip

    parts = []
    for role, text in turns:
        if len(text) > 2000:
            text = text[:2000] + " ...[truncated]"
        parts.append(f"{role}: {text}")
    digest = "\n\n".join(parts)
    if len(digest) > max_chars:
        digest = (digest[: max_chars // 4]
                  + "\n\n...[middle of session omitted]...\n\n"
                  + digest[-(max_chars * 3 // 4):])

    duration_min = None
    if first_ts and last_ts and last_ts > first_ts:
        duration_min = round((last_ts - first_ts).total_seconds() / 60)

    return {
        "digest": digest,
        "user_msgs": user_msgs,
        "assistant_msgs": assistant_msgs,
        "tool_counts": tool_counts,
        "files_touched": files_touched,
        "bash_cmds": bash_cmds,
        "first_prompt": first_prompt,
        "model": model,
        "branch": "",
        "duration_min": duration_min,
        "codex_session_id": meta_sid,
        "codex_cwd": meta_cwd,
        "started_at": first_ts.isoformat() if first_ts else "",
    }


def find_latest_rollout():
    """Newest Codex rollout under $CODEX_HOME/sessions (default ~/.codex)."""
    base = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "sessions"
    if not base.exists():
        return None
    candidates = []
    for pat in ("rollout-*.jsonl", "rollout-*.jsonl.zst"):
        candidates.extend(base.rglob(pat))
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


# ------------------------------------------------------------- summarization

SUMMARY_PROMPT = (
    "You are writing an entry for a developer's engineering journal. The stdin "
    "contains a digest of one coding-agent session (USER/ASSISTANT turns plus "
    "[tool: ...] actions). Respond with ONLY the note, shaped exactly like this: "
    "first line is a specific 4-8 word title (no quotes, no 'Session' prefix), "
    "then a blank line, then markdown with exactly these five sections: "
    "'## What happened', '## Decisions', '## Changes', '## Lessons', "
    "'## Open threads'. Lessons are only durable, reusable facts worth "
    "remembering in future sessions of this project (environment quirks, "
    "commands that worked, traps hit) - one short bullet each, and '- none' "
    "if the session taught nothing reusable. Past tense, information-dense, "
    "no preamble, no closing remarks, no code fences around the whole thing. "
    "If any section is empty write '- none'. Under 380 words total."
)


def find_claude_cli():
    path = shutil.which("claude")
    if path:
        return path
    for candidate in ("~/.local/bin/claude", "/usr/local/bin/claude",
                      "/opt/homebrew/bin/claude", "~/.claude/local/claude"):
        p = Path(candidate).expanduser()
        if p.exists():
            return str(p)
    return None


def ai_summary(digest: str, meta_header: str, model: str):
    """Return (title, body_md) from `claude -p`, or None to fall back."""
    cli = find_claude_cli()
    if not cli:
        log("claude CLI not found; using structural digest")
        return None
    env = dict(os.environ, DEKYON_ACTIVE="1")
    # Hooks inherit CLAUDECODE from the host, and the claude CLI refuses to
    # run nested when it's set. Recursion is already prevented by
    # DEKYON_ACTIVE plus disableAllHooks, so drop the flag for the child.
    env.pop("CLAUDECODE", None)
    try:
        p = subprocess.run(
            [cli, "-p", SUMMARY_PROMPT, "--model", model,
             "--settings", '{"disableAllHooks": true}'],
            input=meta_header + "\n\n" + digest,
            capture_output=True, text=True, timeout=300, env=env,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        log(f"claude -p failed to run: {e}")
        return None
    if p.returncode != 0 or not p.stdout.strip():
        log(f"claude -p rc={p.returncode}: {p.stderr.strip()[:300]}")
        return None
    out = p.stdout.strip()
    out = re.sub(r"^```(?:markdown)?\s*|\s*```$", "", out).strip()
    lines = out.splitlines()
    title = lines[0].lstrip("# ").strip().strip('"')
    body = "\n".join(lines[1:]).strip()
    if not title or not body:
        return None
    return title, body


def structural_summary(stats: dict):
    title = " ".join(stats.get("first_prompt", "").split())[:60] or "Untitled session"
    first = stats.get("first_prompt", "")[:300]
    lines = ["## What happened",
             f"- Started from: \"{first}\"",
             f"- {stats['user_msgs']} user / {stats['assistant_msgs']} assistant messages",
             "", "## Decisions", "- (no AI summary; structural digest only)",
             "", "## Changes"]
    lines += [f"- `{f}`" for f in stats.get("files_touched", [])] or ["- none recorded"]
    if stats.get("bash_cmds"):
        lines += ["", "### Commands run"] + [f"- `{c}`" for c in stats["bash_cmds"]]
    lines += ["", "## Lessons", "- none",
              "", "## Open threads", "- review this session's transcript if needed"]
    return title, "\n".join(lines)


# --------------------------------------------------------------------- notes

def build_note(payload: dict, stats: dict, title: str, body: str, cfg: dict,
               kind: str = "session", stable_name: bool = False):
    now = datetime.now().astimezone()
    title = " ".join(str(title).split())[:100] or "Untitled session"
    cwd = str(payload.get("cwd", ""))
    project = slugify(Path(cwd).name if cwd else "misc", 32)
    sid = re.sub(r"[^A-Za-z0-9_-]", "", str(payload.get("session_id", "")))[:12] or "nosid"
    if stable_name:  # Codex Stop upserts rewrite one file per session
        started = parse_ts(stats.get("started_at")) or now
        prompt_slug = slugify(stats.get("first_prompt", "") or title)
        fname = f"{started:%Y-%m-%d}--{started:%H%M}--codex--{prompt_slug}--{sid}.md"
    else:
        fname = f"{now:%Y-%m-%d}--{now:%H%M}--{slugify(title)}--{sid}.md"
    rel_path = f"sessions/{project}/{fname}"

    tools = ", ".join(f"{k} x{v}" for k, v in
                      sorted(stats.get("tool_counts", {}).items(), key=lambda x: -x[1])[:6])
    def yaml_string(value) -> str:
        return json.dumps(str(value), ensure_ascii=False)

    def safe_meta(value) -> str:
        value = str(value)
        return redact(value) if cfg.get("redact", True) else value

    dur = stats.get("duration_min")
    fm = [
        "---",
        f"title: {yaml_string(safe_meta(title))}",
        f"date: {now.isoformat(timespec='minutes')}",
        f"session_id: {yaml_string(safe_meta(payload.get('session_id', '')))}",
        f"project: {yaml_string(project)}",
        f"cwd: {yaml_string(safe_meta(cwd))}",
        f"branch: {yaml_string(safe_meta(stats.get('branch') or 'unknown'))}",
        f"reason: {yaml_string(safe_meta(payload.get('reason', 'unknown')))}",
        f"kind: {yaml_string(kind)}",
        f"model: {yaml_string(safe_meta(stats.get('model') or 'unknown'))}",
        f"duration_min: {dur if dur is not None else 'unknown'}",
        f"messages: {{user: {stats.get('user_msgs', 0)}, assistant: {stats.get('assistant_msgs', 0)}}}",
        "---", "",
        f"# {title}", "",
        body, "",
    ]
    if stats.get("files_touched") and "## Changes" not in body:
        fm.append("Files: " + ", ".join(f"`{f}`" for f in stats["files_touched"][:15]))
        fm.append("")
    fm += ["---",
           f"*Auto-captured by dekyon . tools: {tools or 'none'} . "
           f"files touched: {len(stats.get('files_touched', []))}*"]
    return rel_path, "\n".join(fm) + "\n"


def update_index(repo: Path, rel_path: str, title: str, stats: dict, payload: dict) -> Path:
    note = Path(rel_path)
    index = repo / note.parent / "index.md"
    now = datetime.now().astimezone()
    line = (f"- {now:%Y-%m-%d %H:%M} . [{title}]({note.name}) . "
            f"{stats.get('user_msgs', 0)}+{stats.get('assistant_msgs', 0)} msgs . "
            f"{payload.get('reason', '?')}\n")
    if not index.exists():
        index.parent.mkdir(parents=True, exist_ok=True)
        index.write_text(f"# Sessions . {note.parent.name}\n\nNewest first.\n\n", encoding="utf-8")
    content = index.read_text(encoding="utf-8")
    if f"]({note.name})" in content:  # upsert: replace the old line for this note
        content = "\n".join(l for l in content.splitlines()
                            if f"]({note.name})" not in l) + "\n"
    marker = "Newest first.\n\n"
    if marker in content:
        content = content.replace(marker, marker + line, 1)
    else:
        content += line
    index.write_text(content, encoding="utf-8")
    return index


def append_lessons(repo: Path, rel_path: str, title: str, body: str):
    """Harvest '## Lessons' bullets into a rolling per-project ledger
    (sessions/<project>/lessons.md). The idea is borrowed from ECC's
    continuous-learning instincts, minus the machinery: plain dated markdown
    lines that the SessionStart injector can tail. Returns the ledger's
    repo-relative path when something was appended, else None."""
    m = re.search(r"## Lessons\s*([\s\S]*?)(?=\n## |\n---|\Z)", body)
    if not m:
        return None
    bullets = [l.strip()[2:].strip() for l in m.group(1).splitlines()
               if l.strip().startswith("- ")]
    bullets = [b for b in bullets if b and b.lower() not in ("none", "none.")]
    if not bullets:
        return None
    note = Path(rel_path)
    ledger = repo / note.parent / "lessons.md"
    if not ledger.exists():
        ledger.parent.mkdir(parents=True, exist_ok=True)
        ledger.write_text(f"# Lessons . {note.parent.name}\n\n"
                          "Durable, reusable takeaways harvested from session "
                          "notes; newest last.\n\n", encoding="utf-8")
    existing = ledger.read_text(encoding="utf-8", errors="replace").splitlines()
    stamp = datetime.now().astimezone().strftime("%Y-%m-%d")
    appended = False
    with ledger.open("a", encoding="utf-8") as f:
        for b in bullets[:5]:
            if any(f". {b} . [" in line and line.endswith(f"]({note.name})")
                   for line in existing):
                continue
            line = f"- {stamp} . {b} . [{title[:40]}]({note.name})"
            f.write(line + "\n")
            existing.append(line)
            appended = True
    return str(ledger.relative_to(repo)) if appended else None


# ----------------------------------------------------------------------- git

def git_commit_push(repo: Path, rel_paths: list, message: str, cfg: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = STATE_DIR / "git.lock"
    with git_lock(lock_path):
        if not (repo / ".git").exists():
            rc, _, err = run(["git", "init", "-b", "main"], cwd=repo)
            log(f"git init {repo}: rc={rc} {err}")

        ident = []
        rc, email, _ = run(["git", "config", "user.email"], cwd=repo)
        if rc != 0 or not email:
            ident = ["-c", "user.name=dekyon", "-c", "user.email=dekyon@localhost"]

        run(["git", "add"] + rel_paths, cwd=repo)
        rc, out, err = run(["git"] + ident + ["commit", "-m", message], cwd=repo)
        if rc != 0:
            log(f"git commit failed: {err or out}")
            return
        log(f"committed: {message}")

        if not cfg.get("push", True):
            return
        rc, remotes, _ = run(["git", "remote"], cwd=repo)
        remote = cfg.get("remote", "origin")
        if remote not in remotes.split():
            log(f"no remote '{remote}' configured; commit kept local "
                f"(add one with: git -C {repo} remote add {remote} <github-url>)")
            return
        branch = cfg.get("branch") or ""
        if not branch:
            _, branch, _ = run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo)
            branch = branch or "main"
        rc, out, err = run(
            ["git", "pull", "--rebase", "--autostash", remote, branch],
            cwd=repo,
            timeout=60,
        )
        if rc != 0:
            # A conflicted rebase can poison every later capture. Abort any
            # rebase state and keep the new commit local for a future retry.
            run(["git", "rebase", "--abort"], cwd=repo, timeout=15)
            log(f"git pull failed; commit kept local: {(err or out)[:200]}")
            return
        rc, out, err = run(["git", "push", remote, f"HEAD:{branch}"], cwd=repo, timeout=60)
        log(f"git push rc={rc} {(err or out)[:200]}" if rc else f"pushed to {remote}/{branch}")


# ---------------------------------------------------------------------- main

def main() -> int:
    args = sys.argv[1:]
    dry_run = "--dry-run" in args
    upsert = "--upsert" in args
    args = [a for a in args if a not in ("--dry-run", "--upsert")]

    if args and args[0] != "--stdin":
        payload_path = Path(args[0])
        try:
            payload = json.loads(payload_path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as e:
            log(f"cannot read payload {args[0]}: {e}")
            return 0
    else:
        payload_path = None
        # utf-8-sig: PowerShell pipes JSON with a UTF-8 BOM, which the
        # README's manual-test command hits on Windows.
        raw = (sys.stdin.buffer.read().decode("utf-8-sig")
               if hasattr(sys.stdin, "buffer") else sys.stdin.read())
        payload = json.loads(raw.lstrip("\ufeff"))

    time.sleep(1.5)  # let the host finish flushing the transcript
    cfg = load_config()

    event = payload.get("hook_event_name") or ""
    kind = "session"
    if event == "PreCompact":
        kind = "precompact"
        payload["reason"] = "precompact-" + str(payload.get("trigger") or "auto")
    elif upsert:
        kind = "codex"
        payload.setdefault("reason", "checkpoint")

    reason = payload.get("reason", "")
    if reason in cfg.get("skip_reasons", []):
        log(f"skipping session {payload.get('session_id')}: reason '{reason}' in skip_reasons")
        return _cleanup(payload_path)

    if upsert:  # Codex Stop fires per turn; don't rewrite the note every time
        interval = int(cfg.get("codex_stop_min_interval", 240))
        sid_key = re.sub(r"[^A-Za-z0-9]", "", str(payload.get("session_id") or "anon"))[:24]
        stamp = STATE_DIR / f"upsert-{sid_key or 'anon'}.ts"
        try:
            if interval > 0 and stamp.exists() and \
                    time.time() - stamp.stat().st_mtime < interval:
                log(f"upsert throttled for session {payload.get('session_id')} "
                    f"(< {interval}s since last)")
                return _cleanup(payload_path)
        except OSError:
            pass

    raw_tpath = str(payload.get("transcript_path") or "").strip()
    tpath = Path(raw_tpath).expanduser() if raw_tpath else None
    if tpath is None or not tpath.is_file():
        found = find_latest_rollout() if (upsert or payload.get("source") == "codex"
                                          or event == "Stop") else None
        if found:
            log(f"transcript missing from payload; using newest rollout {found}")
            tpath = found
        else:
            log(f"transcript not found: {raw_tpath or '<missing>'}")
            return _cleanup(payload_path)

    stats = parse_transcript(tpath, int(cfg["max_transcript_chars"]))
    if stats:  # Codex rollouts carry their own session id / cwd in session_meta
        if not payload.get("session_id") and stats.get("codex_session_id"):
            payload["session_id"] = stats["codex_session_id"]
        if not payload.get("cwd") and stats.get("codex_cwd"):
            payload["cwd"] = stats["codex_cwd"]
    is_codex = bool(stats.get("codex_session_id")) if stats else False
    is_codex = is_codex or payload.get("source") == "codex"
    if is_codex and kind == "session":
        kind = "codex"
    if not stats or stats["user_msgs"] < int(cfg["min_user_messages"]):
        log(f"skipping trivial session {payload.get('session_id')} "
            f"({stats.get('user_msgs', 0) if stats else 0} user messages)")
        return _cleanup(payload_path)

    if cfg.get("redact", True):
        stats["digest"] = redact(stats["digest"])
        stats["first_prompt"] = redact(stats["first_prompt"])

    meta = (f"project: {Path(payload.get('cwd', '')).name} | branch: {stats.get('branch') or '?'} | "
            f"duration: {stats.get('duration_min') or '?'} min | "
            f"messages: {stats['user_msgs']} user / {stats['assistant_msgs']} assistant")
    if cfg.get("redact", True):
        meta = redact(meta)

    result = None
    want_ai = cfg.get("summarizer", "claude") == "claude"
    if upsert and not cfg.get("codex_ai_upserts", False):
        want_ai = False  # per-turn upserts stay cheap and deterministic
    if want_ai:
        result = ai_summary(stats["digest"], meta, cfg.get("model", "haiku"))
    title, body = result if result else structural_summary(stats)
    if cfg.get("redact", True):
        title, body = redact(title), redact(body)

    rel_path, note_md = build_note(payload, stats, title, body, cfg,
                                   kind=kind, stable_name=(upsert or is_codex))

    if dry_run:
        print(f"--- would write {rel_path} ---\n{note_md}")
        return _cleanup(payload_path)

    repo = Path(cfg["repo_dir"]).expanduser()
    note_file = repo / rel_path
    note_file.parent.mkdir(parents=True, exist_ok=True)
    note_file.write_text(note_md, encoding="utf-8")
    index = update_index(repo, rel_path, title, stats, payload)
    log(f"wrote {note_file}")

    to_commit = [rel_path, str(index.relative_to(repo))]
    if cfg.get("lessons", True):
        ledger = append_lessons(repo, rel_path, title, body)
        if ledger:
            to_commit.append(ledger)
            log(f"lessons appended to {ledger}")

    if upsert:
        try:
            stamp.parent.mkdir(parents=True, exist_ok=True)
            stamp.write_text(str(time.time()), encoding="utf-8")
        except OSError:
            pass

    project = Path(rel_path).parts[1]
    suffix = {"precompact": " [compact]", "codex": " [update]"}.get(kind, "")
    git_commit_push(repo, to_commit, f"session({project}): {title}{suffix}", cfg)
    return _cleanup(payload_path)


def _cleanup(payload_path) -> int:
    if payload_path:
        try:
            Path(payload_path).unlink(missing_ok=True)
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # last-resort: log, never crash loudly
        log(f"unhandled error: {type(e).__name__}: {e}")
        sys.exit(0)
