"""Review-gated local Plugin installation with integrity and rollback records."""

from __future__ import annotations

import json
import shutil
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from packages.contracts.validation import plugin_integrity, validate_plugin


class PluginInstallError(ValueError):
    """Raised when a Plugin is invalid, unreviewed or unsafe to install."""


@dataclass(frozen=True)
class PluginRecord:
    name: str
    version: str
    publisher: str
    integrity_sha256: str
    installed_at: str
    source: str
    review_status: str
    requested_tools: list[str] = field(default_factory=list)
    requested_network: list[str] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)


class PluginRegistry:
    """Filesystem registry for local preview and air-gapped development.

    The registry never executes Plugin code. Installation only copies validated
    package files and records the requested permissions for a later runtime
    review. A production registry should add signature verification and a
    transactional shared store.
    """

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._rollback_root = self.root / ".rollback"
        self._rollback_root.mkdir(parents=True, exist_ok=True)
        self._registry_path = self.root / "registry.json"
        self._events_path = self.root / "events.jsonl"
        if not self._registry_path.exists():
            self._write_records({})

    def list_installed(self) -> list[PluginRecord]:
        records = self._read_records()
        return [PluginRecord(**records[name]) for name in sorted(records)]

    def install(
        self,
        source: str | Path,
        *,
        approved_by: str,
        expected_integrity: str | None = None,
    ) -> PluginRecord:
        source_path = Path(source).resolve()
        self._assert_safe_source(source_path)
        if not approved_by.strip():
            raise PluginInstallError("Plugin installation requires an explicit reviewer")
        report = validate_plugin(source_path)
        if not report.valid:
            raise PluginInstallError("Plugin validation failed: " + "; ".join(report.issues))
        manifest = json.loads(
            (source_path / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
        )
        integrity = plugin_integrity(source_path)
        if expected_integrity is not None and expected_integrity != integrity:
            raise PluginInstallError("Plugin integrity does not match the expected digest")
        name = str(manifest["name"])
        version = str(manifest["version"])
        destination = (self.root / name).resolve()
        self._assert_contained(destination)
        rollback_path: Path | None = None
        if destination.exists():
            rollback_path = (
                self._rollback_root
                / f"{name}-{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}-{uuid4().hex}"
            )
            shutil.move(str(destination), str(rollback_path))
        staging = self.root / f".staging-{name}-{uuid4().hex}"
        try:
            shutil.copytree(source_path, staging, symlinks=False)
            staging.replace(destination)
        except Exception:
            if staging.exists():
                shutil.rmtree(staging)
            if rollback_path is not None and not destination.exists():
                shutil.move(str(rollback_path), str(destination))
            raise
        records = self._read_records()
        record = PluginRecord(
            name=name,
            version=version,
            publisher=str(manifest["publisher"]),
            integrity_sha256=integrity,
            installed_at=datetime.now(UTC).isoformat(),
            source=source_path.as_posix(),
            review_status=f"approved:{approved_by.strip()}",
            requested_tools=list(manifest.get("permissions", {}).get("tools", [])),
            requested_network=list(manifest.get("permissions", {}).get("network", [])),
            dependencies=list(manifest.get("dependencies", [])),
        )
        records[name] = asdict(record)
        self._write_records(records)
        self._append_event("install", name, version, approved_by.strip())
        return record

    def rollback(self, name: str) -> PluginRecord:
        destination = (self.root / name).resolve()
        self._assert_contained(destination)
        candidates = sorted(
            (
                path
                for path in self._rollback_root.glob(f"{name}-*")
                if "-current-" not in path.name
            ),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        if not candidates:
            raise PluginInstallError(f"no rollback version is available for {name}")
        if destination.exists():
            current = self._rollback_root / f"{name}-current-{uuid4().hex}"
            shutil.move(str(destination), str(current))
        shutil.copytree(candidates[0], destination, symlinks=False)
        manifest = json.loads(
            (destination / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
        )
        record = PluginRecord(
            name=name,
            version=str(manifest["version"]),
            publisher=str(manifest["publisher"]),
            integrity_sha256=plugin_integrity(destination),
            installed_at=datetime.now(UTC).isoformat(),
            source=destination.as_posix(),
            review_status="rollback-approved",
            requested_tools=list(manifest.get("permissions", {}).get("tools", [])),
            requested_network=list(manifest.get("permissions", {}).get("network", [])),
            dependencies=list(manifest.get("dependencies", [])),
        )
        records = self._read_records()
        records[name] = asdict(record)
        self._write_records(records)
        self._append_event("rollback", name, str(manifest["version"]), "rollback-approved")
        return record

    def remove(self, name: str, *, approved_by: str) -> None:
        """Remove an installed Plugin while retaining a rollback copy."""

        if not approved_by.strip():
            raise PluginInstallError("Plugin removal requires an explicit reviewer")
        destination = (self.root / name).resolve()
        self._assert_contained(destination)
        if not destination.is_dir():
            raise PluginInstallError(f"installed Plugin was not found: {name}")
        backup = (
            self._rollback_root
            / f"{name}-removed-{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}-{uuid4().hex}"
        )
        shutil.move(str(destination), str(backup))
        records = self._read_records()
        records.pop(name, None)
        self._write_records(records)
        self._append_event("remove", name, "unknown", approved_by.strip())

    def _read_records(self) -> dict[str, dict[str, str]]:
        try:
            raw = json.loads(self._registry_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PluginInstallError(f"cannot read Plugin registry: {exc}") from exc
        if not isinstance(raw, dict):
            raise PluginInstallError("Plugin registry is malformed")
        return raw

    def _write_records(self, records: dict[str, dict[str, str]]) -> None:
        temporary = self._registry_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(records, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temporary.replace(self._registry_path)

    def _append_event(self, action: str, name: str, version: str, actor: str) -> None:
        event = {
            "action": action,
            "name": name,
            "version": version,
            "actor": actor,
            "created_at": datetime.now(UTC).isoformat(),
        }
        with self._events_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, sort_keys=True) + "\n")

    def _assert_safe_source(self, source: Path) -> None:
        if not source.is_dir():
            raise PluginInstallError("Plugin source must be a directory")
        if any(path.is_symlink() for path in source.rglob("*")):
            raise PluginInstallError("Plugin symlinks are not allowed")

    def _assert_contained(self, path: Path) -> None:
        try:
            path.relative_to(self.root)
        except ValueError as exc:
            raise PluginInstallError("Plugin path escapes registry root") from exc
