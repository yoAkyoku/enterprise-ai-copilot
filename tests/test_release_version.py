from __future__ import annotations

import unittest
from pathlib import Path

from scripts.check_release_version import package_version, validate_release_tag

ROOT = Path(__file__).resolve().parents[1]


class ReleaseVersionTests(unittest.TestCase):
    def test_current_package_version_is_loaded(self) -> None:
        self.assertRegex(package_version(ROOT), r"^\d+\.\d+\.\d+(?:\.dev\d+)?$")

    def test_release_tag_requires_stable_semver_and_exact_match(self) -> None:
        validate_release_tag("v0.2.0", "0.2.0")
        with self.assertRaises(ValueError):
            validate_release_tag("v0.2.0", "0.2.0.dev0")
        with self.assertRaises(ValueError):
            validate_release_tag("v0.2.1", "0.2.0")

    def test_release_tag_rejects_non_semver_tags(self) -> None:
        with self.assertRaises(ValueError):
            validate_release_tag("release-0.2.0", "0.2.0")
        with self.assertRaises(ValueError):
            validate_release_tag("v0.2.0-rc.1", "0.2.0-rc.1")


if __name__ == "__main__":
    unittest.main()
