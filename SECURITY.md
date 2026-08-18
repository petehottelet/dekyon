# Security policy

## Reporting a vulnerability

Please report security issues privately through
[GitHub Security Advisories](https://github.com/petehottelet/dekyon/security/advisories/new).
Do not open a public issue for a vulnerability or include real transcripts,
tokens, credentials, or proprietary source code in a report.

Include the affected dekyon version, host agent and version, operating system,
reproduction steps, and the smallest sanitized example that demonstrates the
issue. You should receive an acknowledgement within seven days.

## Sensitive session data

Session notes are derived from coding-agent transcripts. Keep the notes repo
private unless its contents are intentionally public. Built-in redaction is
best-effort and cannot guarantee removal of every secret or proprietary value.
With the default summarizer, a redacted digest is sent through the authenticated
Claude CLI; set `"summarizer": "none"` in `~/.claude/dekyon.json` to use only
the local structural digest.
