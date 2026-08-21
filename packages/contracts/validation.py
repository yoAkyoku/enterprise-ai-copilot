"""Fail-closed validation for repository extension contracts.

The validator intentionally accepts a small, explicit subset of YAML. It uses
``yaml.safe_load`` and never evaluates config as code. The returned report is
JSON-serializable so the CLI and HTTP API can expose the same result.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


class ContractValidationError(ValueError):
    """Raised when a contract cannot be loaded or violates the schema."""


@dataclass
class ValidationReport:
    kind: str
    path: str
    issues: list[str] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return not self.issues

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "path": self.path,
            "valid": self.valid,
            "issues": list(self.issues),
        }


_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")
_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$")
_RISK_VALUES = {"read", "write", "external_send", "destructive"}
_TRANSPORT_VALUES = {"in_memory", "stdio", "streamable_http"}
_ARGUMENT_TYPES = {"string", "number", "boolean", "object", "array"}
_MAX_TEXT = 4000


def _read_yaml(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return yaml.safe_load(handle)
    except (OSError, yaml.YAMLError) as exc:
        raise ContractValidationError(f"cannot load YAML {path}: {exc}") from exc


def _read_json(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractValidationError(f"cannot load JSON {path}: {exc}") from exc


def _base_report(kind: str, path: Path) -> ValidationReport:
    return ValidationReport(kind=kind, path=path.as_posix())


def _require_mapping(
    value: Any, report: ValidationReport, field_name: str
) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        report.issues.append(f"{field_name} must be a mapping")
        return None
    return value


def _require_string(
    mapping: dict[str, Any],
    report: ValidationReport,
    field_name: str,
    *,
    max_length: int = _MAX_TEXT,
) -> str | None:
    value = mapping.get(field_name)
    if not isinstance(value, str) or not value.strip():
        report.issues.append(f"{field_name} must be a non-empty string")
        return None
    if len(value) > max_length:
        report.issues.append(f"{field_name} exceeds {max_length} characters")
    return value.strip()


def _check_id_and_version(mapping: dict[str, Any], report: ValidationReport) -> None:
    identifier = _require_string(mapping, report, "id", max_length=63)
    if identifier and not _ID_RE.fullmatch(identifier):
        report.issues.append("id must contain lowercase letters, numbers and hyphens only")
    version = _require_string(mapping, report, "version", max_length=64)
    if version and not _SEMVER_RE.fullmatch(version):
        report.issues.append("version must use semantic-version format")


def _validate_list_of_strings(
    mapping: dict[str, Any], report: ValidationReport, field_name: str, *, required: bool = False
) -> list[str]:
    value = mapping.get(field_name)
    if value is None and not required:
        return []
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        report.issues.append(f"{field_name} must be a list of non-empty strings")
        return []
    return [item.strip() for item in value]


def _contained_file(
    base: Path, relative: Any, report: ValidationReport, field_name: str
) -> Path | None:
    if not isinstance(relative, str) or not relative.strip():
        report.issues.append(f"{field_name} must be a relative path")
        return None
    candidate = Path(relative)
    if candidate.is_absolute() or ".." in candidate.parts:
        report.issues.append(f"{field_name} must remain inside its contract directory")
        return None
    resolved_base = base.resolve()
    resolved = (base / candidate).resolve()
    try:
        resolved.relative_to(resolved_base)
    except ValueError:
        report.issues.append(f"{field_name} escapes its contract directory")
        return None
    if not resolved.is_file():
        report.issues.append(f"{field_name} does not point to a readable file")
        return None
    return resolved


def validate_agent_manifest(path: str | Path) -> ValidationReport:
    manifest_path = Path(path)
    report = _base_report("agent", manifest_path)
    try:
        raw = _read_yaml(manifest_path)
    except ContractValidationError as exc:
        report.issues.append(str(exc))
        return report
    mapping = _require_mapping(raw, report, "manifest")
    if mapping is None:
        return report
    for field_name in ("id", "version", "name", "description", "instructions"):
        _require_string(mapping, report, field_name)
    _check_id_and_version(mapping, report)
    _contained_file(manifest_path.parent, mapping.get("instructions"), report, "instructions")
    skills = mapping.get("skills")
    if skills is not None and not isinstance(skills, dict):
        report.issues.append("skills must be a mapping with an allow list")
    elif isinstance(skills, dict):
        _validate_list_of_strings(skills, report, "allow")
    mcp = mapping.get("mcp")
    if mcp is not None and not isinstance(mcp, dict):
        report.issues.append("mcp must be a mapping with an allow list")
    elif isinstance(mcp, dict):
        _validate_list_of_strings(mcp, report, "allow")
    approval = mapping.get("approval")
    if not isinstance(approval, dict):
        report.issues.append("approval must explicitly define read/write/external_send/destructive")
    else:
        expected = {"read", "write", "external_send", "destructive"}
        missing = expected - set(approval)
        if missing:
            report.issues.append(f"approval is missing: {', '.join(sorted(missing))}")
        for key, value in approval.items():
            if key in expected and value not in {"auto", "required", "deny"}:
                report.issues.append(f"approval.{key} must be auto, required or deny")
    limits = mapping.get("limits")
    if not isinstance(limits, dict):
        report.issues.append("limits must be a mapping")
    else:
        for key in ("max_steps", "max_runtime_seconds"):
            value = limits.get(key)
            if not isinstance(value, int) or value <= 0:
                report.issues.append(f"limits.{key} must be a positive integer")
    return report


def validate_skill(path: str | Path) -> ValidationReport:
    skill_path = Path(path)
    report = _base_report("skill", skill_path)
    try:
        text = skill_path.read_text(encoding="utf-8")
    except OSError as exc:
        report.issues.append(f"cannot read skill: {exc}")
        return report
    if len(text) > 64 * 1024:
        report.issues.append("skill exceeds 64 KiB")
    if not text.startswith("---\n"):
        report.issues.append("skill must start with YAML frontmatter")
        return report
    closing = text.find("\n---", 4)
    if closing < 0:
        report.issues.append("skill frontmatter is not closed")
        return report
    try:
        frontmatter = yaml.safe_load(text[4:closing])
    except yaml.YAMLError as exc:
        report.issues.append(f"invalid skill frontmatter: {exc}")
        return report
    mapping = _require_mapping(frontmatter, report, "frontmatter")
    if mapping is not None:
        _require_string(mapping, report, "name", max_length=63)
        _require_string(mapping, report, "description", max_length=1000)
    if not text[closing + 4 :].strip():
        report.issues.append("skill body must not be empty")
    return report


def validate_mcp_config(path: str | Path) -> ValidationReport:
    config_path = Path(path)
    report = _base_report("mcp", config_path)
    try:
        raw = _read_yaml(config_path)
    except ContractValidationError as exc:
        report.issues.append(str(exc))
        return report
    mapping = _require_mapping(raw, report, "config")
    if mapping is None:
        return report
    servers = mapping.get("servers")
    if not isinstance(servers, dict) or not servers:
        report.issues.append("servers must be a non-empty mapping")
        return report
    seen_tools: set[str] = set()
    for server_id, server in servers.items():
        if not isinstance(server_id, str) or not _ID_RE.fullmatch(server_id):
            report.issues.append(f"server id {server_id!r} is invalid")
        if not isinstance(server, dict):
            report.issues.append(f"server {server_id} must be a mapping")
            continue
        transport = server.get("transport")
        if transport not in _TRANSPORT_VALUES:
            report.issues.append(f"server {server_id} has unsupported transport")
        tools = server.get("tools")
        if not isinstance(tools, list) or not tools:
            report.issues.append(f"server {server_id} must declare tools")
            continue
        for tool in tools:
            if not isinstance(tool, dict):
                report.issues.append(f"server {server_id} has a malformed tool")
                continue
            name = tool.get("name")
            if not isinstance(name, str) or not re.fullmatch(r"^[a-z0-9_.-]+$", name):
                report.issues.append(f"server {server_id} has an invalid tool name")
            elif name in seen_tools:
                report.issues.append(f"duplicate tool name: {name}")
            else:
                seen_tools.add(name)
            if tool.get("risk") not in _RISK_VALUES:
                report.issues.append(f"tool {name!r} has an invalid risk class")
            roles = tool.get("allow_roles")
            if not isinstance(roles, list) or any(not isinstance(role, str) for role in roles):
                report.issues.append(f"tool {name!r} must declare allow_roles")
            if not isinstance(tool.get("external_write"), bool):
                report.issues.append(f"tool {name!r} must declare external_write")
            argument_schema = tool.get("argument_schema")
            if not isinstance(argument_schema, dict) or any(
                not isinstance(argument_name, str)
                or not re.fullmatch(r"^[a-z][a-z0-9_]{0,63}$", argument_name)
                or argument_type not in _ARGUMENT_TYPES
                for argument_name, argument_type in argument_schema.items()
            ):
                report.issues.append(f"tool {name!r} must declare a valid argument_schema mapping")
    return report


def validate_schedule(path: str | Path) -> ValidationReport:
    schedule_path = Path(path)
    report = _base_report("schedule", schedule_path)
    try:
        raw = _read_yaml(schedule_path)
    except ContractValidationError as exc:
        report.issues.append(str(exc))
        return report
    mapping = _require_mapping(raw, report, "schedule")
    if mapping is None:
        return report
    for field_name in ("id", "version", "agent"):
        _require_string(mapping, report, field_name, max_length=128)
    _check_id_and_version(mapping, report)
    schedule = mapping.get("schedule")
    if not isinstance(schedule, dict):
        report.issues.append("schedule must be a mapping")
    else:
        schedule_type = schedule.get("type")
        if schedule_type not in {"one_shot", "interval", "cron"}:
            report.issues.append("schedule.type must be one_shot, interval or cron")
        if schedule_type == "cron" and (
            not isinstance(schedule.get("expression"), str)
            or len(schedule["expression"].split()) != 5
        ):
            report.issues.append("cron schedule.expression must contain five fields")
        if schedule_type == "interval" and (
            not isinstance(schedule.get("seconds"), int) or schedule["seconds"] <= 0
        ):
            report.issues.append("interval schedule.seconds must be a positive integer")
        timezone_name = schedule.get("timezone", "UTC")
        if not isinstance(timezone_name, str) or not timezone_name.strip():
            report.issues.append("schedule.timezone must be a non-empty string")
    run = mapping.get("run")
    if not isinstance(run, dict):
        report.issues.append("run must be a mapping")
    else:
        for key in ("max_runtime_seconds", "max_concurrency", "retry_limit"):
            if not isinstance(run.get(key), int) or run[key] < 0:
                report.issues.append(f"run.{key} must be a non-negative integer")
        if run.get("max_concurrency", 0) == 0:
            report.issues.append("run.max_concurrency must be greater than zero")
        if run.get("mode", "isolated") not in {"isolated", "shared"}:
            report.issues.append("run.mode must be isolated or shared")
        if run.get("query") is not None and (
            not isinstance(run["query"], str)
            or not run["query"].strip()
            or len(run["query"]) > 4000
        ):
            report.issues.append("run.query must be a non-empty string of at most 4000 characters")
        if run.get("order_id") is not None and (
            not isinstance(run["order_id"], str)
            or not run["order_id"].strip()
            or len(run["order_id"]) > 128
        ):
            report.issues.append(
                "run.order_id must be a non-empty string of at most 128 characters"
            )
        if run.get("allow_external_processing") is not None and not isinstance(
            run["allow_external_processing"], bool
        ):
            report.issues.append("run.allow_external_processing must be a boolean")
    permissions = mapping.get("permissions")
    if not isinstance(permissions, dict) or permissions.get("mode") not in {
        "read_only",
        "approved_write",
    }:
        report.issues.append("permissions.mode must be read_only or approved_write")
    notify = mapping.get("notify", {})
    if not isinstance(notify, dict):
        report.issues.append("notify must be a mapping")
    else:
        channel = notify.get("channel")
        if channel is not None and (
            not isinstance(channel, str) or not channel.strip() or len(channel) > 64
        ):
            report.issues.append(
                "notify.channel must be a non-empty string of at most 64 characters"
            )
        if notify.get("only_if", "finding_or_failure") not in {
            "always",
            "finding_or_failure",
            "failure",
        }:
            report.issues.append("notify.only_if must be always, finding_or_failure or failure")
    return report


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def plugin_integrity(plugin_dir: str | Path) -> str:
    root = Path(plugin_dir).resolve()
    digest = hashlib.sha256()
    for path in sorted(
        item for item in root.rglob("*") if item.is_file() and ".git" not in item.parts
    ):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(relative)
        digest.update(_file_digest(path).encode("ascii"))
    return digest.hexdigest()


def plugin_signature_digest(plugin_dir: str | Path) -> str:
    """Return the digest signed by a Plugin publisher.

    The signature field itself is removed from the manifest before hashing so
    the signature does not create a circular dependency. The ordinary
    ``plugin_integrity`` value still covers the exact installed bytes.
    """

    root = Path(plugin_dir).resolve()
    digest = hashlib.sha256()
    for path in sorted(
        item for item in root.rglob("*") if item.is_file() and ".git" not in item.parts
    ):
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8"))
        if relative == ".codex-plugin/plugin.json":
            try:
                manifest = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ContractValidationError(
                    f"cannot read Plugin manifest for signature digest: {exc}"
                ) from exc
            signing = manifest.get("signing")
            if isinstance(signing, dict):
                signing = dict(signing)
                signing.pop("signature", None)
                manifest = dict(manifest)
                manifest["signing"] = signing
            content = json.dumps(
                manifest, ensure_ascii=True, separators=(",", ":"), sort_keys=True
            ).encode("utf-8")
            digest.update(hashlib.sha256(content).hexdigest().encode("ascii"))
        else:
            digest.update(_file_digest(path).encode("ascii"))
    return digest.hexdigest()


def validate_plugin(path: str | Path) -> ValidationReport:
    plugin_dir = Path(path)
    manifest_path = plugin_dir / ".codex-plugin" / "plugin.json"
    report = _base_report("plugin", plugin_dir)
    if not plugin_dir.is_dir():
        report.issues.append("plugin directory does not exist")
        return report
    try:
        raw = _read_json(manifest_path)
    except ContractValidationError as exc:
        report.issues.append(str(exc))
        return report
    mapping = _require_mapping(raw, report, "plugin manifest")
    if mapping is None:
        return report
    for field_name in ("name", "version", "description", "publisher"):
        _require_string(mapping, report, field_name, max_length=200)
    plugin_name = mapping.get("name")
    if isinstance(plugin_name, str) and not _ID_RE.fullmatch(plugin_name):
        report.issues.append("name must contain lowercase letters, numbers and hyphens only")
    version = mapping.get("version")
    if isinstance(version, str) and not _SEMVER_RE.fullmatch(version):
        report.issues.append("version must use semantic-version format")
    for field_name in ("skills", "mcp"):
        value = mapping.get(field_name)
        if value is not None:
            target = _contained_file_or_directory(plugin_dir, value, report, field_name)
            if target is not None and not target.is_dir():
                report.issues.append(f"{field_name} must point to a directory")
            elif target is not None and field_name == "skills":
                for skill_path in sorted(target.rglob("SKILL.md")):
                    skill_report = validate_skill(skill_path)
                    report.issues.extend(
                        f"{skill_path.name}: {issue}" for issue in skill_report.issues
                    )
            elif target is not None and field_name == "mcp":
                for mcp_path in sorted((*target.glob("*.yaml"), *target.glob("*.yml"))):
                    mcp_report = validate_mcp_config(mcp_path)
                    report.issues.extend(f"{mcp_path.name}: {issue}" for issue in mcp_report.issues)
    permissions = mapping.get("permissions")
    if not isinstance(permissions, dict):
        report.issues.append("permissions must be a mapping")
    else:
        for key in ("network", "tools"):
            value = permissions.get(key, [])
            if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
                report.issues.append(f"permissions.{key} must be a list of strings")
    dependencies = mapping.get("dependencies", [])
    if not isinstance(dependencies, list) or any(
        not isinstance(item, str) for item in dependencies
    ):
        report.issues.append("dependencies must be a list of strings")
    signing = mapping.get("signing")
    if signing is not None:
        if not isinstance(signing, dict):
            report.issues.append("signing must be a mapping")
        else:
            for field_name in ("key_id", "signature"):
                value = signing.get(field_name)
                if not isinstance(value, str) or not value.strip():
                    report.issues.append(f"signing.{field_name} must be a non-empty string")
                elif len(value) > 4096:
                    report.issues.append(f"signing.{field_name} exceeds 4096 characters")
    return report


def _contained_file_or_directory(
    base: Path, relative: Any, report: ValidationReport, field_name: str
) -> Path | None:
    if not isinstance(relative, str) or not relative.strip():
        report.issues.append(f"{field_name} must be a relative path")
        return None
    candidate = Path(relative)
    if candidate.is_absolute() or ".." in candidate.parts:
        report.issues.append(f"{field_name} must stay inside plugin directory")
        return None
    resolved_base = base.resolve()
    resolved = (base / candidate).resolve()
    try:
        resolved.relative_to(resolved_base)
    except ValueError:
        report.issues.append(f"{field_name} escapes plugin directory")
        return None
    if not resolved.exists():
        report.issues.append(f"{field_name} does not exist")
        return None
    return resolved


def validate_repository(root: str | Path) -> list[ValidationReport]:
    repository = Path(root).resolve()
    reports: list[ValidationReport] = []
    for manifest in sorted(repository.glob("agents/*/agent.yaml")):
        reports.append(validate_agent_manifest(manifest))
    for skill in sorted(repository.glob(".agents/skills/*/SKILL.md")):
        reports.append(validate_skill(skill))
    mcp_config = repository / "mcp" / "servers.yaml"
    if mcp_config.exists():
        reports.append(validate_mcp_config(mcp_config))
    schedule_paths = sorted(repository.glob("schedules/*.yaml")) + sorted(
        repository.glob("schedules/*.yml")
    )
    for schedule in schedule_paths:
        reports.append(validate_schedule(schedule))
    for plugin in sorted(repository.glob("plugins/*")):
        if plugin.is_dir():
            reports.append(validate_plugin(plugin))
    return reports
