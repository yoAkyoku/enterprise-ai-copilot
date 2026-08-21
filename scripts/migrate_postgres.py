"""Apply checked-in PostgreSQL migrations with a transaction per run."""

from __future__ import annotations

import argparse
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = sorted((ROOT / "infra" / "migrations" / "postgres").glob("*.sql"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Apply Enterprise Agent PostgreSQL migrations")
    parser.add_argument("database_url", nargs="?", default=os.getenv("AGENT_DATABASE_URL", ""))
    args = parser.parse_args(argv)
    if not args.database_url.strip():
        parser.error("database URL is required as an argument or AGENT_DATABASE_URL")
    try:
        import psycopg
    except ImportError as exc:
        raise RuntimeError("psycopg is required for PostgreSQL migrations") from exc
    if not MIGRATIONS:
        raise RuntimeError("no PostgreSQL migrations are present")
    with psycopg.connect(args.database_url, connect_timeout=10) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations "
                "(version TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW())"
            )
            for migration in MIGRATIONS:
                version = migration.stem
                cursor.execute("SELECT 1 FROM schema_migrations WHERE version = %s", (version,))
                if cursor.fetchone():
                    continue
                cursor.execute(migration.read_text(encoding="utf-8"))
                cursor.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (version,))
    print(f"postgres migrations applied: {len(MIGRATIONS)} discovered")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
