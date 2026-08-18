import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import contextmanager, redirect_stdout
from importlib import util
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = util.spec_from_file_location("dekyon_worker", ROOT / "scripts" / "dekyon_worker.py")
worker = util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(worker)


class DekyonWorkerTests(unittest.TestCase):
    @staticmethod
    def git(cwd, *args):
        return subprocess.run(
            ["git", *args], cwd=cwd, check=True, capture_output=True,
            text=True, encoding="utf-8", errors="replace",
        ).stdout.strip()

    def test_import_and_lock_are_cross_platform(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = Path(tmp) / "state" / "git.lock"
            with worker.git_lock(lock_path):
                self.assertTrue(lock_path.exists())

    def test_parse_claude_transcript_ignores_tool_results(self):
        rows = [
            {
                "type": "user",
                "timestamp": "2026-08-17T10:00:00Z",
                "message": {"content": [{"type": "text", "text": "Fix auth"}]},
            },
            {
                "type": "assistant",
                "timestamp": "2026-08-17T10:02:00Z",
                "message": {
                    "model": "claude-test",
                    "content": [
                        {"type": "text", "text": "I found the issue."},
                        {
                            "type": "tool_use",
                            "name": "Edit",
                            "input": {"file_path": "src/auth.py"},
                        },
                    ],
                },
            },
            {
                "type": "user",
                "timestamp": "2026-08-17T10:03:00Z",
                "message": {"content": [{"type": "tool_result", "content": "ok"}]},
            },
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "claude.jsonl"
            path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
            stats = worker.parse_transcript(path, 10_000)

        self.assertEqual(stats["user_msgs"], 1)
        self.assertEqual(stats["assistant_msgs"], 1)
        self.assertEqual(stats["files_touched"], ["src/auth.py"])
        self.assertEqual(stats["duration_min"], 3)

    def test_parse_codex_rollout_recovers_session_metadata(self):
        rows = [
            {
                "timestamp": "2026-08-17T10:00:00Z",
                "type": "session_meta",
                "payload": {"id": "thr_123456789", "cwd": "/work/api"},
            },
            {
                "timestamp": "2026-08-17T10:01:00Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Fix auth"}],
                },
            },
            {
                "timestamp": "2026-08-17T10:03:00Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "Done"}],
                },
            },
        ]
        lines = [json.dumps(row) for row in rows]
        stats = worker.parse_codex_rollout(lines, 10_000)

        self.assertEqual(stats["codex_session_id"], "thr_123456789")
        self.assertEqual(stats["codex_cwd"], "/work/api")
        self.assertEqual(stats["first_prompt"], "Fix auth")
        self.assertEqual(stats["duration_min"], 3)

    def test_parse_codex_exec_command_uses_current_cmd_argument(self):
        rows = [
            {
                "timestamp": "2026-08-17T10:00:00Z",
                "type": "response_item",
                "payload": {
                    "type": "message", "role": "user",
                    "content": [{"type": "input_text", "text": "Check status"}],
                },
            },
            {
                "timestamp": "2026-08-17T10:01:00Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call", "name": "exec_command",
                    "arguments": json.dumps({"cmd": "git status --short"}),
                },
            },
        ]
        stats = worker.parse_codex_rollout((json.dumps(row) for row in rows), 10_000)

        self.assertEqual(stats["bash_cmds"], ["git status --short"])

    def test_codex_note_path_is_stable_when_summary_title_changes(self):
        payload = {
            "session_id": "thr_123456789",
            "cwd": "/work/api",
            "reason": "other",
        }
        stats = {
            "first_prompt": "Fix auth refresh",
            "started_at": "2026-08-17T10:00:00+00:00",
            "user_msgs": 1,
            "assistant_msgs": 1,
        }
        cfg = {"redact": True}
        first, _ = worker.build_note(payload, stats, "Early title", "## Lessons\n- none", cfg,
                                     kind="codex", stable_name=True)
        final, _ = worker.build_note(payload, stats, "Final AI title", "## Lessons\n- none", cfg,
                                     kind="codex", stable_name=True)

        self.assertEqual(first, final)
        self.assertIn("2026-08-17--1000--codex--fix-auth-refresh--thr_12345678.md", first)

    def test_note_frontmatter_escapes_windows_paths_and_quotes(self):
        payload = {
            "session_id": "abc",
            "cwd": 'C:\\Users\\Pete\\A "quoted" project',
            "reason": "other",
        }
        stats = {"user_msgs": 1, "assistant_msgs": 1, "branch": "feature/test"}
        _, note = worker.build_note(payload, stats, 'A "quoted" title', "## Lessons\n- none",
                                    {"redact": True})

        self.assertIn('title: "A \\"quoted\\" title"', note)
        self.assertIn('cwd: "C:\\\\Users\\\\Pete\\\\A \\"quoted\\" project"', note)

    def test_missing_transcript_uses_latest_codex_rollout(self):
        rows = [
            {
                "timestamp": "2026-08-17T10:00:00Z",
                "type": "session_meta",
                "payload": {"id": "thr_123456789", "cwd": "/work/api"},
            },
            {
                "timestamp": "2026-08-17T10:01:00Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Fix auth"}],
                },
            },
        ]
        with tempfile.TemporaryDirectory() as tmp:
            rollout = Path(tmp) / "rollout-test.jsonl"
            rollout.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
            cfg = dict(worker.DEFAULTS, summarizer="none", push=False)
            stdin = io.StringIO(json.dumps({"source": "codex", "transcript_path": ""}))
            stdout = io.StringIO()
            with mock.patch.object(worker, "find_latest_rollout", return_value=rollout), \
                    mock.patch.object(worker, "load_config", return_value=cfg), \
                    mock.patch.object(worker, "log"), \
                    mock.patch.object(worker.time, "sleep"), \
                    mock.patch.object(sys, "argv", ["dekyon_worker.py", "--stdin", "--dry-run"]), \
                    mock.patch.object(sys, "stdin", stdin), redirect_stdout(stdout):
                rc = worker.main()

        self.assertEqual(rc, 0)
        self.assertIn("--- would write sessions/api/", stdout.getvalue())

    def test_lessons_are_not_duplicated_for_the_same_note(self):
        body = "## Lessons\n- Tokens expire in seconds\n\n## Open threads\n- none"
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            rel = "sessions/api/note.md"
            first = worker.append_lessons(repo, rel, "Auth fix", body)
            second = worker.append_lessons(repo, rel, "Different title", body)
            ledger = (repo / "sessions" / "api" / "lessons.md").read_text(encoding="utf-8")

        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertEqual(ledger.count("Tokens expire in seconds"), 1)

    def test_ai_summary_unsets_claudecode_for_nested_cli(self):
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            captured.update(kwargs)
            return mock.Mock(returncode=0,
                             stdout="A title\n\n## What happened\n- x",
                             stderr="")

        with mock.patch.object(worker.shutil, "which", return_value="claude"), \
                mock.patch.object(worker.subprocess, "run", side_effect=fake_run), \
                mock.patch.dict(worker.os.environ, {"CLAUDECODE": "1"}):
            result = worker.ai_summary("digest", "meta", "haiku")

        self.assertIsNotNone(result)
        self.assertNotIn("CLAUDECODE", captured["env"])
        self.assertEqual(captured["env"].get("DEKYON_ACTIVE"), "1")
        # cp1252 (the Windows locale default) cannot encode common digest
        # characters like check marks; the call must pin UTF-8.
        self.assertEqual(captured.get("encoding"), "utf-8")
        self.assertEqual(captured["cmd"][captured["cmd"].index("--tools") + 1], "")
        self.assertEqual(
            captured["cmd"][captured["cmd"].index("--disallowedTools") + 1], "mcp__*"
        )
        self.assertIn("--disable-slash-commands", captured["cmd"])
        self.assertIn("--no-session-persistence", captured["cmd"])
        self.assertIn("<untrusted_session_digest>", captured["input"])

    def test_digest_is_bounded_while_favoring_the_end(self):
        rows = [{
            "type": "user", "timestamp": "2026-08-17T10:00:00Z",
            "message": {"content": "start"},
        }]
        rows.extend({
            "type": "assistant", "timestamp": "2026-08-17T10:00:01Z",
            "message": {"content": [{"type": "text", "text": f"turn-{i}-" + "x" * 80}]},
        } for i in range(200))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "large.jsonl"
            path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
            stats = worker.parse_transcript(path, 500)

        self.assertIn("middle of session omitted", stats["digest"])
        self.assertIn("turn-199", stats["digest"])
        self.assertLess(len(stats["digest"]), 600)

    def test_redaction_covers_common_tokens(self):
        value = "token=ghp_" + ("a" * 32)
        self.assertEqual(worker.redact(value), "[REDACTED]")

    def test_valid_non_object_config_falls_back_to_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "dekyon.json"
            config.write_text("[]\n", encoding="utf-8")
            with mock.patch.object(worker, "CONFIG_PATH", config), \
                    mock.patch.object(worker, "STATE_DIR", root / "state"), \
                    mock.patch.object(worker, "LOG_FILE", root / "state" / "dekyon.log"):
                cfg = worker.load_config()

        self.assertEqual(cfg["summarizer"], worker.DEFAULTS["summarizer"])
        self.assertEqual(cfg["repo_dir"], worker.DEFAULTS["repo_dir"])

    @unittest.skipUnless(shutil.which("zstd"), "zstd executable is not installed")
    def test_zstd_transcript_is_streamed_and_parsed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plain = root / "transcript.jsonl"
            compressed = root / "transcript.jsonl.zst"
            plain.write_text(json.dumps({
                "type": "user", "message": {"content": "Compressed session"},
            }) + "\n", encoding="utf-8")
            subprocess.run(
                [shutil.which("zstd"), "-q", "-f", str(plain), "-o", str(compressed)],
                check=True, capture_output=True,
            )

            stats = worker.parse_transcript(compressed, 4000)

        self.assertEqual(stats["user_msgs"], 1)
        self.assertIn("Compressed session", stats["digest"])

    def test_failed_pull_aborts_rebase_and_does_not_push(self):
        commands = []

        def fake_run(cmd, **_kwargs):
            commands.append(cmd)
            if cmd[:3] == ["git", "config", "user.email"]:
                return 0, "dev@example.test", ""
            if cmd[:2] == ["git", "remote"]:
                return 0, "origin", ""
            if cmd[:3] == ["git", "ls-remote", "--heads"]:
                return 0, "abc refs/heads/main", ""
            if cmd[:2] == ["git", "pull"]:
                return 1, "", "rebase conflict"
            return 0, "", ""

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "notes"
            (repo / ".git").mkdir(parents=True)
            with mock.patch.object(worker, "STATE_DIR", root / "state"), \
                    mock.patch.object(worker, "run", side_effect=fake_run), \
                    mock.patch.object(worker, "log"):
                worker.git_commit_push(
                    repo,
                    ["sessions/api/note.md"],
                    "session(api): note",
                    {"push": True, "remote": "origin", "branch": "main"},
                )

        self.assertIn(["git", "rebase", "--abort"], commands)
        self.assertFalse(any(cmd[:2] == ["git", "push"] for cmd in commands))

    def test_initial_push_bootstraps_an_empty_remote(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote = root / "remote.git"
            repo = root / "notes"
            self.git(root, "init", "--bare", str(remote))
            self.git(root, "init", "-b", "main", str(repo))
            self.git(repo, "remote", "add", "origin", str(remote))
            note = repo / "sessions" / "api" / "note.md"
            note.parent.mkdir(parents=True)
            note.write_text("# note\n", encoding="utf-8")
            with mock.patch.object(worker, "STATE_DIR", root / "state"):
                worker.git_commit_push(
                    repo, ["sessions/api/note.md"], "session(api): note",
                    {"push": True, "remote": "origin", "branch": "main"},
                )

            branch = self.git(root, "--git-dir", str(remote), "rev-parse", "refs/heads/main")
            self.assertRegex(branch, r"^[0-9a-f]{40,64}$")

    def test_automated_commit_preserves_unrelated_staged_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "notes"
            self.git(root, "init", "-b", "main", str(repo))
            self.git(repo, "config", "user.name", "Test")
            self.git(repo, "config", "user.email", "test@example.test")
            unrelated = repo / "unrelated.txt"
            unrelated.write_text("base\n", encoding="utf-8")
            self.git(repo, "add", "unrelated.txt")
            self.git(repo, "commit", "-m", "base")
            unrelated.write_text("staged user work\n", encoding="utf-8")
            self.git(repo, "add", "unrelated.txt")
            note = repo / "sessions" / "api" / "note.md"
            note.parent.mkdir(parents=True)
            note.write_text("# note\n", encoding="utf-8")

            with mock.patch.object(worker, "STATE_DIR", root / "state"):
                worker.git_commit_push(
                    repo, ["sessions/api/note.md"], "session(api): note", {"push": False}
                )

            committed = self.git(repo, "show", "--pretty=format:", "--name-only", "HEAD")
            staged = self.git(repo, "diff", "--cached", "--name-only")
            self.assertEqual(committed, "sessions/api/note.md")
            self.assertEqual(staged, "unrelated.txt")

    def test_concurrent_captures_keep_both_index_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "notes"
            repo.mkdir()
            cfg = dict(worker.DEFAULTS, repo_dir=str(repo), push=False, lessons=False)
            errors = []
            transaction_lock = threading.Lock()

            @contextmanager
            def thread_lock(_path):
                with transaction_lock:
                    yield

            def capture(name):
                try:
                    rel = f"sessions/api/{name}.md"
                    worker.persist_note(
                        repo, rel, f"# {name}\n", name, "## Lessons\n- none",
                        {"user_msgs": 1, "assistant_msgs": 1},
                        {"session_id": name, "reason": "other"}, cfg, "session", False,
                    )
                except Exception as exc:  # pragma: no cover - asserted below
                    errors.append(exc)

            with mock.patch.object(worker, "STATE_DIR", root / "state"), \
                    mock.patch.object(worker, "git_lock", side_effect=thread_lock):
                threads = [threading.Thread(target=capture, args=(name,))
                           for name in ("first", "second")]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=10)

            self.assertFalse(errors)
            index = (repo / "sessions" / "api" / "index.md").read_text(encoding="utf-8")
            self.assertIn("[first](first.md)", index)
            self.assertIn("[second](second.md)", index)
            self.assertEqual(self.git(repo, "rev-list", "--count", "HEAD"), "2")

    def test_rollout_fallback_matches_session_instead_of_newest_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sessions = root / "sessions" / "2026" / "08" / "17"
            sessions.mkdir(parents=True)
            first = sessions / "rollout-first.jsonl"
            second = sessions / "rollout-second.jsonl"
            first.write_text(json.dumps({
                "type": "session_meta", "payload": {"id": "thr-first", "cwd": "/a"}
            }) + "\n", encoding="utf-8")
            second.write_text(json.dumps({
                "type": "session_meta", "payload": {"id": "thr-second", "cwd": "/b"}
            }) + "\n", encoding="utf-8")
            os.utime(first, (1, 1))
            os.utime(second, (2, 2))
            with mock.patch.dict(worker.os.environ, {"CODEX_HOME": str(root)}):
                self.assertEqual(worker.find_latest_rollout("thr-first"), first)
                self.assertEqual(worker.find_latest_rollout("", "/b"), second)
                self.assertIsNone(worker.find_latest_rollout("thr-missing"))

    def test_markdown_link_labels_are_escaped(self):
        self.assertEqual(worker.escape_markdown_link_label("a [b] \\ c"),
                         "a \\[b\\] \\\\ c")


if __name__ == "__main__":
    unittest.main()
