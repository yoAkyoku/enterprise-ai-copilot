"""Create and restore a PostgreSQL backup into a separate target database."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tempfile
import urllib.parse
from pathlib import Path


def _connection_details(database_url: str) -> tuple[str, int, str, str, str]:
    parsed = urllib.parse.urlparse(database_url)
    if parsed.scheme not in {"postgres", "postgresql"} or not parsed.hostname:
        raise ValueError("database URL must be a PostgreSQL DSN with a host")
    try:
        port = parsed.port or 5432
    except ValueError as exc:
        raise ValueError("database URL port is invalid") from exc
    username = urllib.parse.unquote(parsed.username or "")
    password = urllib.parse.unquote(parsed.password or "")
    database = urllib.parse.unquote(parsed.path.lstrip("/"))
    values = (parsed.hostname, username, password, database)
    if not username or not database or any("\x00" in value for value in values):
        raise ValueError("database URL is missing a safe user, password or database")
    return parsed.hostname, port, username, password, database


def _run(command: list[str], environment: dict[str, str], timeout_seconds: float) -> str:
    try:
        completed = subprocess.run(
            command,
            check=True,
            env=environment,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("PostgreSQL backup/restore command timed out") from exc
    except subprocess.CalledProcessError as exc:
        raise RuntimeError("PostgreSQL backup/restore command failed") from exc
    return completed.stdout


def backup_and_restore(
    source_database_url: str,
    restore_database_url: str,
    *,
    confirm_live: bool,
    timeout_seconds: float = 300,
) -> int:
    """Verify a custom-format backup in an operator-provided isolated database.

    The backup is held only in a temporary directory and is never returned as an
    artifact. The restore target must not be the same database as the source;
    the operator is responsible for provisioning it empty and isolated.
    """

    if not confirm_live:
        raise RuntimeError("refusing target database operations without --confirm-live")
    if timeout_seconds <= 0 or timeout_seconds > 3600:
        raise ValueError("backup/restore timeout is invalid")
    source_host, source_port, source_user, source_password, source_database = _connection_details(
        source_database_url
    )
    restore_host, restore_port, restore_user, restore_password, restore_database = (
        _connection_details(restore_database_url)
    )
    source_identity = (source_host.lower(), source_port, source_user, source_database)
    restore_identity = (restore_host.lower(), restore_port, restore_user, restore_database)
    if source_identity == restore_identity:
        raise ValueError("restore database must be different from the source database")
    for executable in ("pg_dump", "pg_restore", "psql"):
        if not shutil.which(executable):
            raise RuntimeError(f"{executable} is required for backup/restore validation")

    with tempfile.TemporaryDirectory(prefix="enterprise-agent-pg-") as directory:
        backup_path = Path(directory) / "target.dump"
        source_environment = os.environ.copy()
        source_environment["PGPASSWORD"] = source_password
        source_environment["PGCONNECT_TIMEOUT"] = "10"
        _run(
            [
                "pg_dump",
                "--format=custom",
                "--no-owner",
                "--no-acl",
                "--host",
                source_host,
                "--port",
                str(source_port),
                "--username",
                source_user,
                "--dbname",
                source_database,
                "--file",
                str(backup_path),
            ],
            source_environment,
            timeout_seconds,
        )
        if not backup_path.is_file() or backup_path.stat().st_size <= 0:
            raise RuntimeError("pg_dump did not create a non-empty backup")

        restore_environment = os.environ.copy()
        restore_environment["PGPASSWORD"] = restore_password
        restore_environment["PGCONNECT_TIMEOUT"] = "10"
        _run(
            [
                "pg_restore",
                "--exit-on-error",
                "--no-owner",
                "--no-acl",
                "--host",
                restore_host,
                "--port",
                str(restore_port),
                "--username",
                restore_user,
                "--dbname",
                restore_database,
                str(backup_path),
            ],
            restore_environment,
            timeout_seconds,
        )
        output = _run(
            [
                "psql",
                "--tuples-only",
                "--no-align",
                "--host",
                restore_host,
                "--port",
                str(restore_port),
                "--username",
                restore_user,
                "--dbname",
                restore_database,
                "--command",
                "SELECT count(*) FROM schema_migrations;",
            ],
            restore_environment,
            timeout_seconds,
        ).strip()
        try:
            migration_count = int(output)
        except ValueError as exc:
            raise RuntimeError("restored database returned an invalid migration count") from exc
        if migration_count < 1:
            raise RuntimeError("restored database has no applied migrations")
    return migration_count


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate PostgreSQL backup and isolated restore")
    parser.add_argument("--confirm-live", action="store_true")
    parser.add_argument("--source-database-url", default=os.getenv("AGENT_DATABASE_URL", ""))
    parser.add_argument(
        "--restore-database-url", default=os.getenv("AGENT_RESTORE_DATABASE_URL", "")
    )
    parser.add_argument("--timeout-seconds", type=float, default=300)
    args = parser.parse_args(argv)
    if not args.source_database_url.strip() or not args.restore_database_url.strip():
        parser.error(
            "source and restore database URLs are required as arguments or environment variables"
        )
    migrations = backup_and_restore(
        args.source_database_url,
        args.restore_database_url,
        confirm_live=args.confirm_live,
        timeout_seconds=args.timeout_seconds,
    )
    print(json.dumps({"status": "PASS", "restored_migrations": migrations}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
