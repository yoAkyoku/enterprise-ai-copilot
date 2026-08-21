"""Require a non-development package version that matches a release tag."""

from __future__ import annotations

import argparse
import re
import tomllib
from pathlib import Path

_TAG = re.compile(r"^v(?P<version>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)$")


def package_version(root: str | Path) -> str:
    path = Path(root) / "pyproject.toml"
    document = tomllib.loads(path.read_text(encoding="utf-8"))
    version = document.get("project", {}).get("version")
    if not isinstance(version, str) or not version.strip():
        raise ValueError("pyproject.toml does not define project.version")
    return version.strip()


def validate_release_tag(tag: str, version: str) -> None:
    match = _TAG.fullmatch(tag.strip())
    if match is None:
        raise ValueError("release tag must be vMAJOR.MINOR.PATCH without a development suffix")
    expected = f"{match.group('version')}.{match.group('minor')}.{match.group('patch')}"
    if version != expected:
        raise ValueError(f"release tag {tag!r} does not match package version {version!r}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check package version against a release tag")
    parser.add_argument("--tag", required=True, help="version tag such as v0.2.0")
    parser.add_argument(
        "--root", default=Path(__file__).resolve().parents[1], type=Path, help=argparse.SUPPRESS
    )
    args = parser.parse_args(argv)
    try:
        version = package_version(args.root)
        validate_release_tag(args.tag, version)
    except (OSError, ValueError, tomllib.TOMLDecodeError) as exc:
        print(f"release-version: FAIL ({exc})")
        return 1
    print(f"release-version: PASS ({args.tag} == {version})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
