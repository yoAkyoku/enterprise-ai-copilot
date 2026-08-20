"""Apply the checked-in SQLite preview migrations."""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = sorted((ROOT / "infra" / "migrations").glob("*.sql"))


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply local Agent Platform migrations")
    parser.add_argument(
        "database", nargs="?", default=str(ROOT / ".data" / "agent-platform.sqlite3")
    )
    args = parser.parse_args()
    database = Path(args.database)
    database.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database)
    try:
        for migration in MIGRATIONS:
            connection.executescript(migration.read_text(encoding="utf-8"))
        connection.commit()
    finally:
        connection.close()
    print(f"migrations applied ({len(MIGRATIONS)}): {database}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
