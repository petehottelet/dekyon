import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from importlib import util
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = util.spec_from_file_location("dekyon_worker", ROOT / "scripts" / "dekyon_worker.py")
worker = util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(worker)


class DekyonWorkerTests(unittest.TestCase):
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

    def test_redaction_covers_common_tokens(self):
        value = "token=ghp_" + ("a" * 32)
        self.assertEqual(worker.redact(value), "[REDACTED]")

    def test_failed_pull_aborts_rebase_and_does_not_push(self):
        commands = []

        def fake_run(cmd, **_kwargs):
            commands.append(cmd)
            if cmd[:3] == ["git", "config", "user.email"]:
                return 0, "dev@example.test", ""
            if cmd[:2] == ["git", "remote"]:
                return 0, "origin", ""
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


if __name__ == "__main__":
    unittest.main()
