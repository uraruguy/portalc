"""portalc.lock — hashes of flows and schemas, last green run.

The lockfile is the claim "this exact flow and this exact schema were green".
That claim is only worth something if it can be checked, so it carries two
kinds of hash:

* per-flow digests of the flow and schema files, so a change to a committed
  artifact after the last green run is visible;
* an `integrity` digest over the lockfile's own contents, so an edit to the
  recorded hashes is visible too.

A lockfile written before integrity digests existed is read as legacy: it is
accepted, reported as unverified, and gets a digest on the next green run.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .errors import ConfigError, Finding, PortalcError
from .schemautil import content_fingerprint
from .workspace import Workspace, write_json

INTEGRITY_FIELD = "integrity"


class LockIntegrityError(PortalcError):
    """portalc.lock does not match itself, or a locked artifact changed.

    Either someone edited the lockfile by hand, or a flow/schema was changed
    without a green run. Both need a human to look at the diff.
    """

    kind = "lock"
    exit_code = 10


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_text(path.read_text())


def lock_digest(lock: dict[str, Any]) -> str:
    """Fingerprint of everything in the lockfile except the digest itself."""
    return content_fingerprint({k: v for k, v in lock.items() if k != INTEGRITY_FIELD})


def load_lock(ws: Workspace, *, verify: bool = True) -> dict[str, Any]:
    if not ws.lockfile_path.is_file():
        return {"apiVersion": "portalc/v1", "portals": {}}
    try:
        lock = json.loads(ws.lockfile_path.read_text())
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{ws.lockfile_path} is not valid JSON", str(exc)) from exc
    if not isinstance(lock, dict):
        raise ConfigError(f"{ws.lockfile_path} should be a JSON object")
    recorded = lock.get(INTEGRITY_FIELD)
    if verify and recorded and recorded != lock_digest(lock):
        raise LockIntegrityError(
            "portalc.lock has been modified since it was written",
            [
                str(ws.lockfile_path),
                f"recorded {recorded}",
                f"computed {lock_digest(lock)}",
            ],
            hint="do not hand-edit portalc.lock: re-run `portalc verify` to rewrite it from a green run",
            data={"recorded": recorded, "computed": lock_digest(lock)},
        )
    return lock


def save_lock(ws: Workspace, lock: dict[str, Any]) -> dict[str, Any]:
    payload = {k: v for k, v in lock.items() if k != INTEGRITY_FIELD}
    payload[INTEGRITY_FIELD] = lock_digest(payload)
    write_json(ws.lockfile_path, payload)
    lock[INTEGRITY_FIELD] = payload[INTEGRITY_FIELD]
    return payload


def bump(version: str, level: str) -> str:
    major, minor, patch = (int(p) for p in version.split("."))
    if level == "major":
        return f"{major + 1}.0.0"
    if level == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


def _artifact_digests(path: Path) -> tuple[str, str]:
    """(raw file sha256, canonical fingerprint) for a JSON artifact."""
    text = path.read_text()
    try:
        canonical = content_fingerprint(json.loads(text))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path} is not valid JSON", str(exc)) from exc
    return sha256_text(text), canonical


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
    flow_sha, flow_fp = _artifact_digests(flow_path)
    schema_sha, schema_fp = _artifact_digests(schema_path)
    entry.setdefault("flows", {})[flow] = {
        "schema": schema_name,
        "flowSha256": flow_sha,
        "schemaSha256": schema_sha,
        "flowFingerprint": flow_fp,
        "schemaFingerprint": schema_fp,
        "lastGreenAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    save_lock(ws, lock)
    return lock


def locked_flow(lock: dict[str, Any], portal: str, flow: str) -> dict[str, Any] | None:
    return ((lock.get("portals") or {}).get(portal, {}).get("flows") or {}).get(flow)


def integrity_findings(
    ws: Workspace,
    portal: str,
    flow: str,
    *,
    flow_path: Path,
    schema_path: Path,
) -> list[Finding]:
    """Compare the committed flow and schema against the last green run."""
    lock = load_lock(ws)
    findings: list[Finding] = []
    if not lock.get(INTEGRITY_FIELD) and ws.lockfile_path.is_file():
        findings.append(
            Finding(
                check="lock.integrity",
                ok=True,
                severity="info",
                message="portalc.lock predates integrity digests — one is added on the next green run",
            )
        )
    entry = locked_flow(lock, portal, flow)
    if entry is None:
        findings.append(
            Finding(
                check="lock.entry",
                ok=True,
                severity="warn",
                message=f"{portal}/{flow} has no lock entry yet (never recorded green)",
            )
        )
        return findings

    for label, path, sha_key, fp_key in (
        ("flow", flow_path, "flowSha256", "flowFingerprint"),
        ("schema", schema_path, "schemaSha256", "schemaFingerprint"),
    ):
        if not path.is_file():
            findings.append(
                Finding(check=f"lock.{label}", ok=False, message=f"{label} file is missing: {path}")
            )
            continue
        sha, fingerprint = _artifact_digests(path)
        locked_sha = entry.get(sha_key)
        locked_fp = entry.get(fp_key)
        if locked_sha == sha or (locked_fp and locked_fp == fingerprint):
            same_bytes = locked_sha == sha
            findings.append(
                Finding(
                    check=f"lock.{label}",
                    ok=True,
                    severity="info" if same_bytes else "warn",
                    message=f"{label} matches the lockfile"
                    + ("" if same_bytes else " (formatting changed, contents did not)"),
                )
            )
        else:
            findings.append(
                Finding(
                    check=f"lock.{label}",
                    ok=False,
                    message=f"{label} file changed since the last green run "
                    f"({entry.get('lastGreenAt', 'unknown date')})",
                    data={"path": str(path), "locked": locked_sha, "onDisk": sha},
                )
            )
    return findings


def assert_integrity(
    ws: Workspace,
    portal: str,
    flow: str,
    *,
    flow_path: Path,
    schema_path: Path,
) -> list[Finding]:
    """Findings, but a mismatch is fatal — for callers that want a frozen run."""
    findings = integrity_findings(ws, portal, flow, flow_path=flow_path, schema_path=schema_path)
    failed = [f for f in findings if not f.ok]
    if failed:
        raise LockIntegrityError(
            failed[0].message,
            [f.message for f in failed],
            hint="run `portalc verify` to re-record the lockfile once you have read the diff",
            data={"findings": [f.as_dict() for f in findings]},
        )
    return findings
