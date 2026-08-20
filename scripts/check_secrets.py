"""Small dependency-free pre-release secret scan for source distributions."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEXT_SUFFIXES = {".py", ".yaml", ".yml", ".json", ".md", ".toml", ".txt", ".ini", ".cfg", ".env"}
PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\b(?:ghp|github_pat|xox[baprs])_[A-Za-z0-9_-]{20,}\b"),
    re.compile(
        r"(?i)\b(?:api[_-]?key|secret|password|access[_-]?token)\s*[:=]\s*['\"][^'\"\n]{12,}['\"]"
    ),
)
IGNORED_PARTS = {".git", ".venv", ".venv-release", "venv", "__pycache__", ".data", "dist", "build"}


def _is_scannable_path(path: str) -> bool:
    name = Path(path).name
    return (
        Path(path).suffix.lower() in TEXT_SUFFIXES
        or name.endswith(".env.example")
        or name in {".env.example", "Dockerfile"}
    )


def _matches(path: str, text: str) -> list[str]:
    if not _is_scannable_path(path):
        return []
    return [pattern.pattern for pattern in PATTERNS if pattern.search(text)]


def _working_tree_findings() -> list[str]:
    findings: list[str] = []
    for path in ROOT.rglob("*"):
        if not path.is_file() or any(part in IGNORED_PARTS for part in path.parts):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for pattern in _matches(path.name, text):
            findings.append(f"{path.relative_to(ROOT).as_posix()}: matched {pattern}")
    return findings


def _git_history_findings() -> tuple[list[str], bool]:
    completed = subprocess.run(
        ["git", "rev-list", "--objects", "--all"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or "git history enumeration failed")
    objects = [line.split(" ", 1) for line in completed.stdout.splitlines() if " " in line]
    if not objects:
        return [], False
    findings: list[str] = []
    for object_id, path in objects:
        if not _is_scannable_path(path):
            continue
        blob = subprocess.run(
            ["git", "cat-file", "blob", object_id],
            cwd=ROOT,
            check=False,
            capture_output=True,
        )
        if blob.returncode != 0:
            continue
        try:
            text = blob.stdout.decode("utf-8")
        except UnicodeDecodeError:
            continue
        for pattern in _matches(path, text):
            findings.append(f"history:{path}: matched {pattern}")
    return findings, True


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Scan source and optional Git history for configured secrets"
    )
    parser.add_argument(
        "--git-history",
        action="store_true",
        help="also scan every text blob reachable from the local Git history",
    )
    args = parser.parse_args()
    findings = _working_tree_findings()
    history_available = True
    if args.git_history:
        try:
            history_findings, history_available = _git_history_findings()
        except RuntimeError as exc:
            print(f"history-scan: {exc}", file=sys.stderr)
            return 2
        findings.extend(history_findings)
        if not history_available:
            print("history-scan: no commits found", file=sys.stderr)
            return 2
    if findings:
        print("Potential secrets found:", file=sys.stderr)
        print("\n".join(findings), file=sys.stderr)
        return 1
    print("secret-scan: no configured secret patterns found")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
