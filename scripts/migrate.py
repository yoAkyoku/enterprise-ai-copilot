"""Apply the checked-in SQLite preview migrations."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = sorted((ROOT / "infra" / "migrations").glob("*.sql"))
NON_IDEMPOTENT_MIGRATIONS = {"005_run_event_tenant_scope.sql"}


def _ensure_audit_tenant_scope(connection: sqlite3.Connection) -> None:
    """Upgrade older SQLite audit tables without requiring manual SQL."""

    columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(run_events)")}
    if "tenant_id" not in columns:
        connection.execute("ALTER TABLE run_events ADD COLUMN tenant_id TEXT")
    rows = connection.execute(
        "SELECT sequence, payload_json FROM run_events WHERE tenant_id IS NULL"
    ).fetchall()
    for sequence, payload_json in rows:
        try:
            payload = json.loads(str(payload_json))
        except (TypeError, ValueError):
            continue
        tenant_id = payload.get("tenant_id") if isinstance(payload, dict) else None
        if isinstance(tenant_id, str) and tenant_id.strip():
            connection.execute(
                "UPDATE run_events SET tenant_id = ? WHERE sequence = ?",
                (tenant_id, sequence),
            )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_run_events_scope "
        "ON run_events(workspace_id, tenant_id, sequence)"
    )


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
            if migration.name in NON_IDEMPOTENT_MIGRATIONS:
                columns = {
                    str(row[1]) for row in connection.execute("PRAGMA table_info(run_events)")
                }
                if "tenant_id" in columns:
                    continue
            connection.executescript(migration.read_text(encoding="utf-8"))
        _ensure_audit_tenant_scope(connection)
        connection.commit()
    finally:
        connection.close()
    print(f"migrations applied ({len(MIGRATIONS)}): {database}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
