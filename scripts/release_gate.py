"""Fail closed when a tagged release still has unresolved evidence gates."""

from __future__ import annotations

import csv
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHECKLIST = ROOT / "docs" / "release" / "v0.2.0-production-track.md"
EVIDENCE = ROOT / "docs" / "validation" / "evidence-index.csv"
ALLOWED_STATUSES = {"PASS", "WAIVED"}
SHA = re.compile(r"^[0-9a-f]{40}$")


def _is_reachable_ancestor(commit: str) -> bool:
    """Require evidence to refer to a real commit reachable from the release."""

    result = subprocess.run(
        ["git", "-c", f"safe.directory={ROOT}", "merge-base", "--is-ancestor", commit, "HEAD"],
        cwd=ROOT,
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


def main() -> int:
    pending = [
        line.strip()
        for line in CHECKLIST.read_text(encoding="utf-8").splitlines()
        if line.strip().startswith("- [ ]")
    ]
    with EVIDENCE.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    malformed = [
        row.get("id", "<unknown>")
        for row in rows
        if set(row)
        != {
            "id",
            "status",
            "scope",
            "commit",
            "command",
            "artifact",
            "notes",
        }
    ]
    unresolved = [
        f"{row.get('id', '<unknown>')}={row.get('status', '<missing>')}"
        for row in rows
        if row.get("status") not in ALLOWED_STATUSES
    ]
    unbound = [
        f"{row.get('id', '<unknown>')}={row.get('commit', '<missing>')}"
        for row in rows
        if not SHA.fullmatch(row.get("commit", ""))
        or not _is_reachable_ancestor(row.get("commit", ""))
    ]
    if malformed:
        print("release-gate: malformed evidence rows: " + ", ".join(malformed), file=sys.stderr)
    if pending:
        print(f"release-gate: {len(pending)} checklist item(s) remain unchecked", file=sys.stderr)
    if unresolved:
        print("release-gate: unresolved evidence: " + ", ".join(unresolved), file=sys.stderr)
    if unbound:
        print(
            "release-gate: evidence is not bound to a full commit SHA: " + ", ".join(unbound),
            file=sys.stderr,
        )
    if malformed or pending or unresolved or unbound:
        return 1
    print(f"release-gate: PASS ({len(rows)} evidence records, all checklist items resolved)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
