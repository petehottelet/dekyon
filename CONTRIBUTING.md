# Contributing to dekyon

Thanks for helping improve dekyon. Bug reports, compatibility notes, tests,
and focused pull requests are welcome.

## Before opening an issue

- Check `~/.claude/dekyon/dekyon.log` for the recorded failure reason.
- Include the dekyon version, host agent and version, operating system, and
  sanitized reproduction steps.
- Never attach real transcripts, credentials, tokens, or proprietary code.
  Report security issues through the private process in [SECURITY.md](SECURITY.md).

## Development

Dekyon requires Python 3.8+ and uses only the standard library at runtime.
From the repository root, run:

```bash
python -m unittest discover -s tests -v
python -m compileall -q install.py uninstall.py scripts
ruff check .
coverage run --branch -m unittest discover -s tests
coverage report --fail-under=70
python scripts/validate_release.py
claude plugin validate .
npx --yes skills@1.5.22 add . --list
```

Keep changes cross-platform. Tests must pass on Windows and Linux, and shell
wrappers must remain POSIX `sh` compatible.

## Pull requests

- Keep each pull request focused and explain the user-visible behavior.
- Add a regression test for bug fixes whenever practical.
- Update `README.md` and `CHANGELOG.md` when behavior or installation changes.
- Do not commit generated caches, local notes, secrets, or anything under
  `00_project_files/`.
