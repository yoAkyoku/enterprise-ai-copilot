"""Repository doctor and contract-validation CLI."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from packages.contracts import validate_repository
from packages.plugins import PluginInstallError, PluginRegistry

ROOT = Path(__file__).resolve().parents[1]


def _reports() -> list[dict[str, object]]:
    return [report.as_dict() for report in validate_repository(ROOT)]


def validate_command() -> int:
    reports = _reports()
    valid = bool(reports) and all(report["valid"] for report in reports)
    print(json.dumps({"valid": valid, "reports": reports}, indent=2))
    return 0 if valid else 1


def doctor_command() -> int:
    reports = _reports()
    checks = {
        "python": sys.version.split()[0],
        "repository": ROOT.is_dir(),
        "agents_file": (ROOT / "AGENTS.md").is_file(),
        "env_example": (ROOT / ".env.example").is_file(),
        "license": (ROOT / "LICENSE").is_file(),
        "docker_compose": (ROOT / "docker-compose.yml").is_file(),
        "contracts_valid": bool(reports) and all(report["valid"] for report in reports),
    }
    print(json.dumps(checks, indent=2))
    return 0 if all(value is True for key, value in checks.items() if key != "python") else 1


def plugin_command(args: argparse.Namespace) -> int:
    trusted_keys: dict[str, str] = {}
    for item in args.trusted_key or []:
        if "=" not in item:
            raise PluginInstallError("--trusted-key must use key-id=base64-public-key")
        key_id, public_key = item.split("=", 1)
        trusted_keys[key_id] = public_key
    registry = PluginRegistry(
        args.registry,
        trusted_publisher_keys=trusted_keys,
        require_signatures=bool(args.require_signature),
    )
    try:
        if args.action == "list":
            print(json.dumps([record.__dict__ for record in registry.list_installed()], indent=2))
            return 0
        record = registry.install(
            args.source,
            approved_by=args.approved_by,
            expected_integrity=args.expected_integrity,
        )
        print(json.dumps(record.__dict__, indent=2))
        return 0
    except PluginInstallError as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Enterprise Agent repository tools")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser(
        "validate", help="validate Agent, Skill, MCP, Plugin and schedule contracts"
    )
    subparsers.add_parser("doctor", help="check local developer-preview prerequisites")
    plugin_parser = subparsers.add_parser(
        "plugin", help="manage the local review-gated Plugin registry"
    )
    plugin_parser.add_argument("action", choices=("list", "install"))
    plugin_parser.add_argument("--registry", default=str(ROOT / ".data" / "plugins"))
    plugin_parser.add_argument("--source")
    plugin_parser.add_argument("--approved-by")
    plugin_parser.add_argument("--expected-integrity")
    plugin_parser.add_argument(
        "--trusted-key",
        action="append",
        default=[],
        help="trusted publisher key as key-id=base64-public-key",
    )
    plugin_parser.add_argument("--require-signature", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "validate":
        return validate_command()
    if args.command == "doctor":
        return doctor_command()
    if args.command == "plugin":
        if args.action == "install" and (not args.source or not args.approved_by):
            parser.error("plugin install requires --source and --approved-by")
        return plugin_command(args)
    parser.error("unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
