#!/usr/bin/env python3
"""Validate the repository surfaces that users and skill indexes consume."""

import json
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ERRORS = []


def require(condition, message):
    if not condition:
        ERRORS.append(message)


def load_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        ERRORS.append(f"{path.relative_to(ROOT)}: {exc}")
        return {}


def skill_frontmatter(path):
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        ERRORS.append(f"{path.relative_to(ROOT)}: {exc}")
        return {}
    match = re.match(r"\A---\s*\n(.*?)\n---\s*\n", text, re.S)
    if not match:
        ERRORS.append(f"{path.relative_to(ROOT)}: missing YAML frontmatter")
        return {}
    fields = {}
    for line in match.group(1).splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            fields[key.strip()] = value.strip().strip('"\'')
    return fields


def main():
    required = [
        ROOT / ".claude-plugin" / "plugin.json",
        ROOT / ".claude-plugin" / "marketplace.json",
        ROOT / "skills" / "dekyon" / "SKILL.md",
        ROOT / "skills" / "dekyon" / "agents" / "openai.yaml",
        ROOT / "assets" / "dekyon_logo_v4_animated.svg",
        ROOT / "LICENSE",
        ROOT / "README.md",
        ROOT / "CONTRIBUTING.md",
        ROOT / "SECURITY.md",
        ROOT / "vercel.json",
    ]
    for path in required:
        require(path.is_file(), f"missing required file: {path.relative_to(ROOT)}")

    plugin = load_json(ROOT / ".claude-plugin" / "plugin.json")
    marketplace = load_json(ROOT / ".claude-plugin" / "marketplace.json")
    entries = marketplace.get("plugins", [])
    require(len(entries) == 1, "marketplace must expose exactly one plugin")
    entry = entries[0] if entries else {}
    require(plugin.get("name") == "dekyon", "plugin name must be dekyon")
    require(entry.get("name") == "dekyon", "marketplace plugin name must be dekyon")
    require(entry.get("source") == "./", "marketplace source must be ./")
    require(plugin.get("version") == entry.get("version"),
            "plugin and marketplace versions must match")
    require(plugin.get("license") == "MIT" and entry.get("license") == "MIT",
            "plugin and marketplace must declare the MIT license")

    version = str(plugin.get("version", ""))
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    require(f"## [{version}]" in changelog,
            f"CHANGELOG.md must contain a {version} release heading")

    fields = skill_frontmatter(ROOT / "skills" / "dekyon" / "SKILL.md")
    require(set(fields) == {"name", "description"},
            "SKILL.md frontmatter must contain only name and description")
    require(fields.get("name") == "dekyon", "skill name must be dekyon")
    require(len(fields.get("description", "")) >= 80,
            "skill description is too short to trigger reliably")

    agent_yaml = (ROOT / "skills" / "dekyon" / "agents" / "openai.yaml").read_text(
        encoding="utf-8"
    )
    for key in ("display_name:", "short_description:", "default_prompt:"):
        require(key in agent_yaml, f"agents/openai.yaml is missing {key[:-1]}")

    logo = (ROOT / "assets" / "dekyon_logo_v4_animated.svg").read_text(
        encoding="utf-8"
    )
    require("<script" not in logo.lower() and "javascript:" not in logo.lower(),
            "public logo must not contain executable script content")

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for needle in (
        "assets/dekyon_logo_v4_animated.svg",
        "actions/workflows/ci.yml",
        "python-3.8%2B",
        "skills.sh/b/petehottelet/dekyon",
        "git clone https://github.com/petehottelet/dekyon.git",
    ):
        require(needle in readme, f"README.md is missing release surface: {needle}")
    require("cp -r dekyon ~/.claude/skills/dekyon" not in readme,
            "README.md still contains the invalid whole-repository skill copy")

    vercel = load_json(ROOT / "vercel.json")
    redirects = vercel.get("redirects", [])
    expected_redirect = {
        "destination": "https://github.com/petehottelet/dekyon",
        "permanent": True,
    }
    require(redirects == [
        {"source": "/", **expected_redirect},
        {"source": "/:path*", **expected_redirect},
    ], "vercel.json must permanently redirect the root and every path to GitHub")

    ignores = {
        line.strip() for line in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    for pattern in ("00_project_files/", ".env", ".env.*", "__pycache__/", ".vercel/"):
        require(pattern in ignores, f".gitignore is missing {pattern}")

    skip_dirs = {".git", "00_project_files", "__pycache__", ".pytest_cache"}
    text_suffixes = {"", ".md", ".py", ".json", ".yaml", ".yml", ".toml", ".sh"}
    for path in ROOT.rglob("*"):
        if not path.is_file() or any(part in skip_dirs for part in path.parts):
            continue
        if path.suffix.lower() not in text_suffixes:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        placeholders = ("YOUR_" + "GITHUB_USERNAME", "dekyon " + "contributors")
        for placeholder in placeholders:
            require(placeholder not in text,
                    f"{path.relative_to(ROOT)} contains placeholder {placeholder}")
        token_pattern = re.compile(
            r"(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
            r"AKIA[0-9A-Z]{16})"
        )
        require(token_pattern.search(text) is None,
                f"{path.relative_to(ROOT)} contains a token-shaped literal")

    if ERRORS:
        for error in ERRORS:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(f"Release package is valid ({version}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
