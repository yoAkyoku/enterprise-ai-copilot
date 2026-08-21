from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts.postgres_backup_restore_smoke import backup_and_restore


class PostgreSQLBackupRestoreSmokeTests(unittest.TestCase):
    def test_backup_and_restore_keeps_credentials_out_of_commands_and_cleans_dump(self) -> None:
        commands: list[list[str]] = []

        def fake_run(
            command: list[str],
            *,
            check: bool,
            env: dict[str, str],
            capture_output: bool,
            text: bool,
            timeout: float,
        ) -> SimpleNamespace:
            self.assertTrue(check)
            self.assertTrue(capture_output)
            self.assertTrue(text)
            self.assertEqual(timeout, 300)
            self.assertNotIn("source-password", command)
            self.assertNotIn("restore-password", command)
            commands.append(command)
            if command[0] == "pg_dump":
                Path(command[command.index("--file") + 1]).write_bytes(b"synthetic dump")
            return SimpleNamespace(stdout="5\n")

        with (
            patch("scripts.postgres_backup_restore_smoke.shutil.which", return_value="tool"),
            patch("scripts.postgres_backup_restore_smoke.subprocess.run", side_effect=fake_run),
        ):
            self.assertEqual(
                backup_and_restore(
                    "postgresql://agent:source-password@source.example.com:5432/platform",
                    "postgresql://agent:restore-password@restore.example.com:5432/platform_restore",
                    confirm_live=True,
                ),
                5,
            )
        self.assertEqual([command[0] for command in commands], ["pg_dump", "pg_restore", "psql"])

    def test_backup_and_restore_rejects_same_database(self) -> None:
        with self.assertRaises(ValueError):
            backup_and_restore(
                "postgresql://agent:secret@db.example.com/platform",
                "postgresql://agent:other-secret@db.example.com/platform",
                confirm_live=True,
            )

    def test_backup_and_restore_requires_explicit_confirmation(self) -> None:
        with self.assertRaises(RuntimeError):
            backup_and_restore(
                "postgresql://agent:secret@source.example.com/platform",
                "postgresql://agent:secret@restore.example.com/platform_restore",
                confirm_live=False,
            )


if __name__ == "__main__":
    unittest.main()
