"""portalc.lock — hashes of flows and schemas, last green run."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .workspace import Workspace, write_json


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_text(path.read_text())


def load_lock(ws: Workspace) -> dict[str, Any]:
    if not ws.lockfile_path.is_file():
        return {"apiVersion": "portalc/v1", "portals": {}}
    return json.loads(ws.lockfile_path.read_text())


def save_lock(ws: Workspace, lock: dict[str, Any]) -> None:
    write_json(ws.lockfile_path, lock)


def bump(version: str, level: str) -> str:
    major, minor, patch = (int(p) for p in version.split("."))
    if level == "major":
        return f"{major + 1}.0.0"
    if level == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


def record_green(
    ws: Workspace,
    portal: str,
    flow: str,
    *,
    schema_name: str,
    version: str,
    flow_path: Path,
    schema_path: Path,
) -> dict[str, Any]:
    lock = load_lock(ws)
    portals = lock.setdefault("portals", {})
    entry = portals.setdefault(portal, {"version": version, "flows": {}})
    entry["version"] = version
    entry.setdefault("flows", {})[flow] = {
        "schema": schema_name,
        "flowSha256": sha256_file(flow_path),
        "schemaSha256": sha256_file(schema_path),
        "lastGreenAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    save_lock(ws, lock)
    return lock
