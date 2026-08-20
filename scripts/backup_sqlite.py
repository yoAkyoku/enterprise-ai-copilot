"""Create and verify a consistent SQLite backup without arbitrary SQL input."""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path


def verify_database(path: str | Path) -> tuple[bool, str]:
    database = Path(path)
    if not database.is_file():
        return False, "database does not exist"
    try:
        connection = sqlite3.connect(database)
        result = connection.execute("PRAGMA integrity_check").fetchone()
        connection.close()
    except sqlite3.Error:
        return False, "database integrity check failed"
    if not result or result[0] != "ok":
        return False, "database integrity check failed"
    return True, "ok"


def backup_database(source: str | Path, destination: str | Path) -> int:
    source_path = Path(source)
    destination_path = Path(destination)
    if source_path.resolve() == destination_path.resolve():
        raise ValueError("backup destination must differ from source")
    valid, message = verify_database(source_path)
    if not valid:
        raise ValueError(message)
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    if destination_path.exists():
        raise FileExistsError("backup destination already exists")
    source_connection = sqlite3.connect(source_path)
    destination_connection = sqlite3.connect(destination_path)
    try:
        source_connection.backup(destination_connection)
        destination_connection.commit()
    finally:
        destination_connection.close()
        source_connection.close()
    valid, message = verify_database(destination_path)
    if not valid:
        raise ValueError(message)
    print(f"backup created: {destination_path}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Create and verify a SQLite platform backup")
    parser.add_argument("source")
    parser.add_argument("destination")
    args = parser.parse_args()
    return backup_database(args.source, args.destination)


if __name__ == "__main__":
    raise SystemExit(main())
