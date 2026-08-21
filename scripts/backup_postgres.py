"""Create a PostgreSQL custom-format backup without exposing the password in argv."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import urllib.parse
from pathlib import Path


def backup(database_url: str, destination: str | Path) -> int:
    parsed = urllib.parse.urlparse(database_url)
    if parsed.scheme not in {"postgres", "postgresql"} or not parsed.hostname:
        raise ValueError("database URL must be a PostgreSQL DSN with a host")
    target = Path(destination).resolve()
    if target.exists():
        raise FileExistsError("backup destination already exists")
    if not shutil.which("pg_dump"):
        raise RuntimeError("pg_dump is required for PostgreSQL backups")
    username = urllib.parse.unquote(parsed.username or "")
    password = urllib.parse.unquote(parsed.password or "")
    database = parsed.path.lstrip("/")
    if (
        not username
        or not database
        or any("\x00" in value for value in (username, password, database))
    ):
        raise ValueError("database URL is missing a safe user, password or database")
    target.parent.mkdir(parents=True, exist_ok=True)
    command = [
        "pg_dump",
        "--format=custom",
        "--no-owner",
        "--no-acl",
        "--host",
        parsed.hostname,
        "--port",
        str(parsed.port or 5432),
        "--username",
        username,
        "--dbname",
        database,
        "--file",
        str(target),
    ]
    environment = os.environ.copy()
    environment["PGPASSWORD"] = password
    try:
        subprocess.run(command, check=True, env=environment, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        raise RuntimeError("pg_dump failed") from exc
    if not target.is_file() or target.stat().st_size <= 0:
        raise RuntimeError("pg_dump did not create a non-empty backup")
    print(f"postgres backup created: {target}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create a PostgreSQL custom-format backup")
    parser.add_argument("destination")
    parser.add_argument("--database-url", default=os.getenv("AGENT_DATABASE_URL", ""))
    args = parser.parse_args(argv)
    if not args.database_url.strip():
        parser.error("database URL is required as --database-url or AGENT_DATABASE_URL")
    return backup(args.database_url, args.destination)


if __name__ == "__main__":
    raise SystemExit(main())
