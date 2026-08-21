from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.backup_postgres import backup


class PostgreSQLBackupTests(unittest.TestCase):
    def test_backup_keeps_password_out_of_pg_dump_arguments(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "backup.dump"

            def fake_run(
                command: list[str],
                *,
                check: bool,
                env: dict[str, str],
                capture_output: bool,
                text: bool,
            ) -> None:
                self.assertTrue(check)
                self.assertTrue(capture_output)
                self.assertTrue(text)
                self.assertNotIn("secret-password", command)
                self.assertEqual(env["PGPASSWORD"], "secret-password")
                output_path = Path(command[command.index("--file") + 1])
                output_path.write_bytes(b"synthetic backup")

            with (
                patch("scripts.backup_postgres.shutil.which", return_value="pg_dump"),
                patch("scripts.backup_postgres.subprocess.run", side_effect=fake_run),
            ):
                self.assertEqual(
                    backup(
                        "postgresql://agent:secret-password@db.example.com:5432/agent",
                        destination,
                    ),
                    0,
                )
            self.assertTrue(destination.is_file())

    def test_backup_rejects_non_postgresql_dsn(self) -> None:
        with self.assertRaises(ValueError):
            backup("https://example.invalid/database", "backup.dump")


if __name__ == "__main__":
    unittest.main()
