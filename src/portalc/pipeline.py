"""Library-side commands. The CLI is a thin Rich skin over these."""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from .codegen import generate_ts_client
from .engine import HttpEngine, dump_evidence, rows_fingerprint
from .envfile import load_workspace_env
from .errors import (
    ConfigError,
    Finding,
    PortalAuthError,
    PortalDownError,
    PortalDriftError,
    UsageError,
    VerificationFailed,
)
from .lockfile import bump, load_lock, record_green, sha256_text
from .schemautil import field_rows, header_findings, schema_from_table, validate_rows
from .workspace import API_VERSION, PortalConfig, Workspace, read_json, write_json

DEMO_PORTAL = "demo-marina"
DEMO_USER = "skipper@demo-marina.test"
DEMO_PASS = "demo-portal-password"
DEFAULT_MONTH = "2026-09"


def _today() -> str:
    return date.today().isoformat()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def prepare(ws: Workspace) -> Workspace:
    load_workspace_env(ws.root)
    return ws


def demo_server_script() -> Path:
    # installed package: look next to repo layout first
    here = Path(__file__).resolve()
    candidates = [
        here.parents[2] / "demo" / "portal" / "server.py",  # src/portalc -> repo
        Path.cwd() / "demo" / "portal" / "server.py",
    ]
    for c in candidates:
        if c.is_file():
            return c
    raise ConfigError(
        "bundled demo portal is not on disk",
        "expected demo/portal/server.py next to the project",
        hint="run portalc from a git clone of portalc, not from a bare wheel",
    )


PORTAL_YAML_DEMO = """\
apiVersion: portalc/v1
name: demo-marina
title: Demo Marina Partner Portal
baseUrl: {base_url}
auth:
  mode: form
  loginPath: /login
  secrets:
    username: DEMO_MARINA_USER
    password: DEMO_MARINA_PASS
policy:
  readOnly: true
  allowedDomains:
    - 127.0.0.1
    - localhost
  timeoutSeconds: 15
  retries: 2
  notes: >
    Bundled synthetic portal. Own-account demo credentials only.
    This is not a real marina and must not be pointed at production systems.
flows:
  - list-invoices
"""

ENV_EXAMPLE = """\
# portalc reads credentials from the environment, never from a tracked file.
#
#   cp .env.example .env     then put your real values in .env
#
# .env is gitignored; .env.example is the only one that belongs in a commit.
# portal.yaml stores the *names* below under auth.secrets — never the values.

# --- bundled Demo Marina portal -------------------------------------------
# Synthetic portal, published credentials, safe to keep as-is. The demo also
# works with no .env at all: portalc falls back to exactly these values.
DEMO_MARINA_USER=skipper@demo-marina.test
DEMO_MARINA_PASS=demo-portal-password

# Only read when you start the demo server directly
# (python demo/portal/server.py). `portalc demo --port` overrides it, and the
# port must match baseUrl in portals/demo-marina/portal.yaml.
DEMO_MARINA_PORT=8787

# --- your own portal ------------------------------------------------------
# One variable per secret named in portals/<name>/portal.yaml, for example:
#
#   ACME_PORTAL_USER=you@example.com
#   ACME_PORTAL_PASS=
#
# In CI these are repository secrets with the same names. Leave real values
# out of this file even as a placeholder — see SECURITY.md.
"""

ACTION_YML = """\
name: portalc verify
on:
  schedule:
    - cron: "0 3 * * *"
  workflow_dispatch:
  push:
    paths:
      - "portals/**"
      - "src/portalc/**"
      - "demo/**"
      - ".github/workflows/portalc.yml"
jobs:
  verify:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - name: Install portalc
        run: pip install -e ".[dev]"
      - name: Start demo portal
        run: |
          python demo/portal/server.py --port 8787 &
          for i in $(seq 1 30); do
            curl -sf http://127.0.0.1:8787/healthz && break
            sleep 0.2
          done
      - name: Verify compiled flows
        env:
          DEMO_MARINA_USER: skipper@demo-marina.test
          DEMO_MARINA_PASS: demo-portal-password
        run: portalc verify --all
"""


def init_demo(ws: Workspace, *, base_url: str = "http://127.0.0.1:8787", force: bool = False) -> dict[str, Any]:
    portal_dir = ws.portal_dir(DEMO_PORTAL)
    yaml_path = portal_dir / "portal.yaml"
    if yaml_path.exists() and not force:
        raise UsageError(
            f"portal {DEMO_PORTAL!r} already exists",
            str(yaml_path),
            hint="nothing to do if you cloned portalc — try `portalc demo`; use --force to re-scaffold, or --path for a new workspace",
        )
    portal_dir.mkdir(parents=True, exist_ok=True)
    (portal_dir / "flows").mkdir(exist_ok=True)
    (portal_dir / "schemas").mkdir(exist_ok=True)
    (portal_dir / "fixtures").mkdir(exist_ok=True)
    (portal_dir / "evidence").mkdir(exist_ok=True)
    yaml_path.write_text(PORTAL_YAML_DEMO.format(base_url=base_url))
    changelog = portal_dir / "CHANGELOG.md"
    if not changelog.exists():
        changelog.write_text(f"# {DEMO_PORTAL}\n\n- {_today()}  workspace created by `portalc init --demo`\n")

    env_ex = ws.root / ".env.example"
    if not env_ex.exists() or force:
        env_ex.write_text(ENV_EXAMPLE)
    env_path = ws.root / ".env"
    if not env_path.exists():
        env_path.write_text(ENV_EXAMPLE)

    gh = ws.root / ".github" / "workflows"
    gh.mkdir(parents=True, exist_ok=True)
    action = gh / "portalc.yml"
    if not action.exists() or force:
        action.write_text(ACTION_YML)

    gitignore = ws.root / ".gitignore"
    gitignore_updated = _ensure_env_gitignored(gitignore)
    return {
        "portal": DEMO_PORTAL,
        "path": str(yaml_path),
        "baseUrl": base_url,
        "gitignore": str(gitignore),
        "gitignoreUpdated": gitignore_updated,
    }


ENV_IGNORE_PATTERNS = {".env", "/.env", ".env*", "*.env", "**/.env"}


def _ensure_env_gitignored(path: Path) -> bool:
    """`init` writes a real .env, so the workspace must ignore it before `git add .`."""
    if not path.exists():
        path.write_text("# written by portalc init — credentials never belong in git\n.env\n")
        return True
    existing = path.read_text()
    entries = {line.strip() for line in existing.splitlines()}
    if entries & ENV_IGNORE_PATTERNS:
        return False
    separator = "" if existing.endswith("\n") or not existing else "\n"
    with path.open("a") as fh:
        fh.write(f"{separator}\n# added by portalc init — credentials never belong in git\n.env\n")
    return True


def _engine_for(
    cfg: PortalConfig,
    engine_name: str = "http",
    engine_options: dict[str, Any] | None = None,
) -> Any:
    """Pick the browser layer. It is a plug-in: the artifacts do not depend on it.

    Both engines get the portal's parsed `policy:` block, so the allowlist and
    the read-only rule hold whichever one a flow was compiled against.
    """
    if engine_name in ("http", "", None):
        return HttpEngine(cfg.base_url, policy=cfg.request_policy)
    if engine_name == "chrome":
        from .chrome import ChromeEngine

        return ChromeEngine.start(cfg.base_url, policy=cfg.request_policy, **(engine_options or {}))
    raise UsageError(
        f"unknown engine {engine_name!r}",
        "portalc has two: http (deterministic HTML) and chrome (real browser over CDP)",
        hint="--engine http   or   --engine chrome",
    )


def _close_engine(engine: Any) -> None:
    close = getattr(engine, "close", None)
    if callable(close):
        close()


def _engine_name(engine: Any) -> str:
    return getattr(engine, "name", "http")


def _browser_seconds(engine: Any, fallback: float) -> float:
    return float(getattr(engine, "browser_seconds", None) or fallback)


def _login(cfg: PortalConfig, engine: Any, *, interactive: bool = False) -> None:
    """Get an authenticated session, without portalc ever owning one it should not.

    `human-session` is the mode a real portal should use: the human signs in in
    the browser window, and later runs inherit that session from the Chrome
    profile. A stale profile surfaces as `PortalAuthError` from the first fetch,
    which is the honest answer — not a failed schema check.
    """
    if cfg.auth_mode == "human-session":
        if interactive and hasattr(engine, "await_human_login"):
            engine.await_human_login(login_path=cfg.auth.get("loginPath") or "/")
        return
    if cfg.name == DEMO_PORTAL:
        user = os.environ.get("DEMO_MARINA_USER", DEMO_USER)
        password = os.environ.get("DEMO_MARINA_PASS", DEMO_PASS)
    else:
        secrets = cfg.resolve_secrets()
        user = secrets.get("username")
        password = secrets.get("password")
        if not user or not password:
            raise PortalAuthError(
                "portal credentials are not in the environment",
                hint="export the env vars named under auth.secrets in portal.yaml",
            )
    path = (cfg.auth.get("loginPath") or "/login")
    if _engine_name(engine) == "chrome":
        engine.login(user, password, path=path, selectors=cfg.auth.get("selectors"))
    else:
        engine.login(user, password, path=path)


def _month(params: dict[str, str]) -> str:
    return params.get("month") or DEFAULT_MONTH


def explore(
    ws: Workspace,
    portal: str,
    flow: str,
    *,
    month: str = DEFAULT_MONTH,
    marker: str = "invoices",
    path: str = "/invoices",
    engine_name: str = "http",
    engine_options: dict[str, Any] | None = None,
    use_agent: bool = False,
    agent_runner: Any = None,
    accept: bool = True,
) -> dict[str, Any]:
    cfg = PortalConfig.load(ws, portal)
    engine = _engine_for(cfg, engine_name, engine_options)
    agent_usd: float | None = None
    agent_notes = ""
    query = {"month": month}
    try:
        _login(cfg, engine, interactive=True)

        if use_agent:
            from . import agent as agent_mod

            proposal = agent_mod.propose(
                agent_mod.explore_task(cfg.base_url, flow, {"month": month}),
                base_url=cfg.base_url,
                cdp_url=getattr(engine, "devtools_url", None),
                allowed_domains=cfg.allowed_domains,
                runner=agent_runner,
            )
            path = proposal.path
            marker = proposal.marker or marker
            query = proposal.query or query
            agent_usd = proposal.usd
            agent_notes = proposal.notes

        runs = []
        for _ in range(2):
            result = engine.fetch_table(path, query=query, marker=marker)
            assert result.table is not None
            runs.append(result)
        screenshot = (
            engine.screenshot_png() if hasattr(engine, "screenshot_png") else b""
        )
        browser_seconds = _browser_seconds(engine, 1.0)
        engine_used = _engine_name(engine)
    finally:
        _close_engine(engine)

    a, b = runs
    if rows_fingerprint(a.table.rows) != rows_fingerprint(b.table.rows):
        raise PortalDriftError(
            "two dry runs of the same flow returned different rows",
            "portalc refuses to compile a flow that is not deterministic",
            hint="pin the query (month, account) and retry; if the portal is genuinely noisy, it is not a v1 candidate",
        )

    table = a.table
    schema_name = "invoice" if marker == "invoices" else marker.rstrip("s")
    schema = schema_from_table(table, schema_name.title().replace("_", " "))
    flow_doc = {
        "apiVersion": API_VERSION,
        "name": flow,
        "schema": schema_name,
        "engine": engine_used,
        "params": {"month": {"type": "string", "default": month}},
        "steps": [
            {"action": "login", "path": cfg.auth.get("loginPath") or "/login"},
            {
                "action": "extract_table",
                "path": path,
                "query": {k: ("{{month}}" if k == "month" else v) for k, v in query.items()},
                "marker": marker,
                "headerContract": table.header_contract(),
            },
        ],
    }
    if agent_notes:
        flow_doc["compiledBy"] = {"agent": True, "notes": agent_notes}
    checks = {
        "minRows": 1,
        "currencyIn": ["EUR"] if "currency" in table.field_names() else None,
    }
    checks = {k: v for k, v in checks.items() if v is not None}

    if not accept:
        return {
            "preview": True,
            "columns": field_rows(schema, table.rows[0] if table.rows else None),
            "rowCount": len(table.rows),
            "schema": schema,
            "flow": flow_doc,
        }

    cfg.flow_path(flow).parent.mkdir(parents=True, exist_ok=True)
    write_json(cfg.flow_path(flow), flow_doc)
    write_json(cfg.checks_path(flow), checks)
    write_json(cfg.schema_path(schema_name), schema)
    fixture = cfg.fixtures_dir() / f"{_today()}.{flow}.json"
    write_json(fixture, table.rows)
    evidence = cfg.evidence_dir() / f"{_today()}.{flow}.html"
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.write_text(dump_evidence(a.html))
    shot_path = None
    if screenshot:
        shot_path = cfg.evidence_dir() / f"{_today()}.{flow}.png"
        shot_path.write_bytes(screenshot)

    changelog = cfg.dir / "CHANGELOG.md"
    with changelog.open("a") as fh:
        fh.write(f"- {_today()}  compiled `{flow}` ({len(table.columns)} fields, {len(table.rows)} rows)\n")

    record_green(
        ws,
        portal,
        flow,
        schema_name=schema_name,
        version=_portal_version(ws, portal),
        flow_path=cfg.flow_path(flow),
        schema_path=cfg.schema_path(schema_name),
    )
    return {
        "preview": False,
        "columns": field_rows(schema, table.rows[0] if table.rows else None),
        "rowCount": len(table.rows),
        "flowPath": str(cfg.flow_path(flow)),
        "schemaPath": str(cfg.schema_path(schema_name)),
        "fixturePath": str(fixture),
        "screenshotPath": str(shot_path) if shot_path else None,
        "dryRuns": 2,
        "engine": engine_used,
        "agentUsd": agent_usd,
        "agentNotes": agent_notes,
        "browserSeconds": browser_seconds,
    }


def _portal_version(ws: Workspace, portal: str) -> str:
    lock = load_lock(ws)
    return lock.get("portals", {}).get(portal, {}).get("version") or "0.1.0"


def run_flow(
    ws: Workspace,
    portal: str,
    flow: str,
    *,
    params: dict[str, str] | None = None,
    validate: bool = True,
    engine_name: str | None = None,
    engine_options: dict[str, Any] | None = None,
    engine: Any = None,
) -> dict[str, Any]:
    cfg = PortalConfig.load(ws, portal)
    flow_doc = read_json(cfg.flow_path(flow), "flow")
    schema_name = flow_doc["schema"]
    schema = read_json(cfg.schema_path(schema_name), "schema")
    values = dict(params or {})
    values.setdefault("month", flow_doc.get("params", {}).get("month", {}).get("default", DEFAULT_MONTH))

    extract_step = next(s for s in flow_doc["steps"] if s["action"] == "extract_table")
    path = extract_step["path"]
    query = {k: _subst(v, values) for k, v in (extract_step.get("query") or {}).items()}
    marker = extract_step.get("marker") or "invoices"

    # A flow remembers which engine compiled it; --engine overrides that. Either
    # way this path is deterministic: no model has a vote in run or verify.
    own_engine = engine is None
    if own_engine:
        engine = _engine_for(cfg, engine_name or flow_doc.get("engine") or "http", engine_options)
    try:
        _login(cfg, engine)
        result = engine.fetch_table(path, query=query, marker=marker)
    finally:
        if own_engine:
            _close_engine(engine)
    table = result.table
    assert table is not None

    findings: list[Finding] = []
    expected_headers = extract_step.get("headerContract")
    if expected_headers is not None and expected_headers != table.header_contract():
        # still compute field-level findings from the schema
        findings.extend(header_findings(schema, table))
        if all(f.ok for f in findings):
            findings.append(
                Finding(
                    check="headers.contract",
                    ok=False,
                    message="live column titles do not match headerContract in the flow file",
                    data={"expected": expected_headers, "live": table.header_contract()},
                )
            )
    else:
        findings.extend(header_findings(schema, table))

    if validate:
        findings.extend(validate_rows(schema, table.rows))
        checks_path = cfg.checks_path(flow)
        if checks_path.is_file():
            checks = read_json(checks_path, "checks")
            min_rows = int(checks.get("minRows") or 0)
            if len(table.rows) < min_rows:
                findings.append(
                    Finding(
                        check="minRows",
                        ok=False,
                        message=f"{len(table.rows)} rows < minRows {min_rows}",
                    )
                )
            else:
                findings.append(
                    Finding(check="minRows", ok=True, message=f"{len(table.rows)} rows (min {min_rows})")
                )

    failed = [f for f in findings if not f.ok and f.severity == "error"]
    return {
        "rows": table.rows,
        "table": table,
        "findings": findings,
        "ok": not failed,
        "html": result.html,
        "schema": schema,
        "schemaName": schema_name,
        "flow": flow_doc,
        "cfg": cfg,
        "engine": _engine_name(engine),
    }


def _subst(value: str, params: dict[str, str]) -> str:
    out = value
    for k, v in params.items():
        out = out.replace("{{" + k + "}}", v)
    return out


def verify(
    ws: Workspace,
    portal: str | None = None,
    flow: str | None = None,
    *,
    params: dict[str, str] | None = None,
    engine_name: str | None = None,
    engine_options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    names = [portal] if portal else ws.list_portals()
    if not names:
        raise UsageError(
            "no portals in this workspace",
            hint="portalc init --demo",
        )
    reports = []
    all_ok = True
    for name in names:
        cfg = PortalConfig.load(ws, name)
        flows = [flow] if flow else _flows_on_disk(cfg)
        if not flows:
            raise UsageError(
                f"portal {name!r} has no compiled flows",
                hint=f"portalc explore {name} --flow list-invoices --yes",
            )
        # One browser per portal, not one per flow: starting Chrome is the
        # expensive part of a nightly run.
        wanted = engine_name or _engine_of_flows(cfg, flows)
        shared = _engine_for(cfg, wanted, engine_options) if wanted == "chrome" else None
        try:
            for fl in flows:
                result = run_flow(
                    ws,
                    name,
                    fl,
                    params=params,
                    validate=True,
                    engine_name=engine_name,
                    engine_options=engine_options,
                    engine=shared,
                )
                reports.append(
                    {"portal": name, "flow": fl, **{k: result[k] for k in ("ok", "findings", "rows", "engine")}}
                )
                if result["ok"]:
                    record_green(
                        ws,
                        name,
                        fl,
                        schema_name=result["schemaName"],
                        version=_portal_version(ws, name),
                        flow_path=cfg.flow_path(fl),
                        schema_path=cfg.schema_path(result["schemaName"]),
                    )
                else:
                    all_ok = False
        finally:
            if shared is not None:
                _close_engine(shared)
    payload = {"ok": all_ok, "reports": reports}
    if not all_ok:
        # pick the first field-level message
        first = next(f for r in reports if not r["ok"] for f in r["findings"] if not f.ok)
        raise VerificationFailed(
            first.message,
            [f"{r['portal']}/{r['flow']}: " + ", ".join(f.message for f in r["findings"] if not f.ok) for r in reports if not r["ok"]],
            hint="portalc repair <portal> --flow <flow>   will re-read the page and write a patch",
            data=payload,
        )
    return payload


def _flows_on_disk(cfg: PortalConfig) -> list[str]:
    d = cfg.dir / "flows"
    if not d.is_dir():
        return []
    return sorted(p.name[: -len(".flow.json")] for p in d.glob("*.flow.json"))


def _engine_of_flows(cfg: PortalConfig, flows: list[str]) -> str:
    """"chrome" only when every flow was compiled against a browser."""
    engines = set()
    for fl in flows:
        path = cfg.flow_path(fl)
        if path.is_file():
            engines.add(read_json(path, "flow").get("engine") or "http")
    return "chrome" if engines == {"chrome"} else "http"


def repair(
    ws: Workspace,
    portal: str,
    flow: str,
    *,
    params: dict[str, str] | None = None,
    engine_name: str | None = None,
    engine_options: dict[str, Any] | None = None,
    use_agent: bool = False,
    agent_runner: Any = None,
) -> dict[str, Any]:
    """Deterministic repair for header renames; `--agent` for anything harder.

    The agent only ever says *where* to look. What it proposes is then read by
    the deterministic engine, checked against the schema and confirmed twice
    before a single file changes.
    """
    cfg = PortalConfig.load(ws, portal)
    before = read_json(cfg.schema_path(read_json(cfg.flow_path(flow), "flow")["schema"]), "schema")
    try:
        live = run_flow(
            ws, portal, flow, params=params, validate=False,
            engine_name=engine_name, engine_options=engine_options,
        )
    except (PortalDownError, PortalDriftError) as exc:
        if not use_agent:
            raise
        return _agent_repair(
            ws,
            portal,
            flow,
            failure=exc.title,
            params=params,
            engine_name=engine_name,
            engine_options=engine_options,
            agent_runner=agent_runner,
        )
    table = live["table"]
    schema = live["schema"]
    flow_doc = live["flow"]
    findings = header_findings(schema, table)

    expected = (schema.get("x-portalc") or {}).get("headers") or {}
    live_headers = {c.name: c.header for c in table.columns}

    # Same field names (data-col or slug), different titles → patch-level rename.
    if set(expected) == set(live_headers) and expected != live_headers:
        new_headers = dict(live_headers)
        schema.setdefault("x-portalc", {})["headers"] = new_headers
        extract_step = next(s for s in flow_doc["steps"] if s["action"] == "extract_table")
        extract_step["headerContract"] = table.header_contract()
        write_json(cfg.schema_path(live["schemaName"]), schema)
        write_json(cfg.flow_path(flow), flow_doc)
        version = bump(_portal_version(ws, portal), "patch")
        lock = load_lock(ws)
        lock.setdefault("portals", {}).setdefault(portal, {})["version"] = version
        from .lockfile import save_lock

        save_lock(ws, lock)
        with (cfg.dir / "CHANGELOG.md").open("a") as fh:
            bits = [f"{k}: {expected[k]!r} → {new_headers[k]!r}" for k in expected if expected[k] != new_headers[k]]
            fh.write(f"- {_today()}  repair `{flow}` (patch {version}): {'; '.join(bits)}\n")

        # confirm
        _confirm_twice(
            ws, portal, flow, params=params, engine_name=engine_name, engine_options=engine_options
        )
        record_green(
            ws,
            portal,
            flow,
            schema_name=live["schemaName"],
            version=version,
            flow_path=cfg.flow_path(flow),
            schema_path=cfg.schema_path(live["schemaName"]),
        )
        pr_body = _pr_body(portal, flow, expected, new_headers, version, findings)
        pr_path = ws.state_dir / f"repair-{portal}-{flow}.md"
        pr_path.parent.mkdir(parents=True, exist_ok=True)
        pr_path.write_text(pr_body)
        diff_lines = []
        for k in expected:
            if expected[k] != new_headers[k]:
                diff_lines.append(f"- {k}: {expected[k]}")
                diff_lines.append(f"+ {k}: {new_headers[k]}")
        generate_ts_client(ws, portal, schema, flow, version)
        return {
            "kind": "rename",
            "version": version,
            "diff": diff_lines,
            "prPath": str(pr_path),
            "prBody": pr_body,
            "confirmations": 2,
            "engine": live.get("engine") or "http",
            "agentUsd": None,
            "browserSeconds": 2.0,
        }

    if use_agent:
        return _agent_repair(
            ws,
            portal,
            flow,
            failure="; ".join(f.message for f in findings if not f.ok) or "the compiled schema no longer matches the page",
            params=params,
            engine_name=engine_name,
            engine_options=engine_options,
            agent_runner=agent_runner,
        )

    raise PortalDriftError(
        "repair cannot auto-patch this drift",
        [f.message for f in findings if not f.ok] or ["field names changed, not just titles"],
        hint="portalc repair --agent  (browser-use, explore/repair only), or re-run portalc explore --yes after reviewing the new schema",
        data={"expected": expected, "live": live_headers},
    )


def _confirm_twice(
    ws: Workspace,
    portal: str,
    flow: str,
    *,
    params: dict[str, str] | None,
    engine_name: str | None,
    engine_options: dict[str, Any] | None,
) -> None:
    """Two green runs in a row, or no patch. One green run is a coincidence."""
    for attempt in (1, 2):
        confirmed = run_flow(
            ws, portal, flow, params=params, validate=True,
            engine_name=engine_name, engine_options=engine_options,
        )
        if not confirmed["ok"]:
            raise PortalDriftError(
                f"the patch did not hold on confirmation {attempt}/2",
                [f.message for f in confirmed["findings"] if not f.ok],
                hint="portalc opens an issue rather than a PR when a repair cannot be confirmed twice",
            )


def _agent_repair(
    ws: Workspace,
    portal: str,
    flow: str,
    *,
    failure: str,
    params: dict[str, str] | None = None,
    engine_name: str | None = None,
    engine_options: dict[str, Any] | None = None,
    agent_runner: Any = None,
) -> dict[str, Any]:
    """Ask an agent where the data moved, then prove it deterministically.

    The agent's answer is a hypothesis. It becomes a patch only if the
    deterministic engine reads the same rows twice at the proposed location and
    the result still satisfies the committed schema.
    """
    from . import agent as agent_mod

    cfg = PortalConfig.load(ws, portal)
    flow_doc = read_json(cfg.flow_path(flow), "flow")
    schema_name = flow_doc["schema"]
    schema = read_json(cfg.schema_path(schema_name), "schema")
    extract_step = next(s for s in flow_doc["steps"] if s["action"] == "extract_table")
    old_path = extract_step["path"]
    old_marker = extract_step.get("marker") or "invoices"
    wanted = engine_name or flow_doc.get("engine") or "http"

    engine = _engine_for(cfg, wanted, engine_options)
    try:
        _login(cfg, engine)
        proposal = agent_mod.propose(
            agent_mod.repair_task(cfg.base_url, flow, failure, old_path),
            base_url=cfg.base_url,
            cdp_url=getattr(engine, "devtools_url", None),
            allowed_domains=cfg.allowed_domains,
            runner=agent_runner,
        )
        month = _month(params or {})
        query = proposal.query or {k: _subst(v, {"month": month}) for k, v in (extract_step.get("query") or {}).items()}
        runs = []
        for _ in range(2):
            result = engine.fetch_table(
                proposal.path, query=query, marker=proposal.marker or old_marker
            )
            assert result.table is not None
            runs.append(result)
        browser_seconds = _browser_seconds(engine, 2.0)
        engine_used = _engine_name(engine)
    finally:
        _close_engine(engine)

    first, second = runs
    if rows_fingerprint(first.table.rows) != rows_fingerprint(second.table.rows):
        raise PortalDriftError(
            "the agent found a page that does not read the same twice",
            [f"proposed path: {proposal.path}", proposal.notes],
            hint="portalc will not compile a non-deterministic flow, from an agent or from you",
        )

    required = set((schema.get("items") or {}).get("required") or [])
    live_fields = set(first.table.field_names())
    if not required.issubset(live_fields):
        raise PortalDriftError(
            "the agent's proposal does not satisfy the committed schema",
            [
                f"missing required fields: {', '.join(sorted(required - live_fields))}",
                f"proposed path: {proposal.path}",
                proposal.notes,
            ],
            hint="this is a schema change, not a repair: review it by hand with portalc explore",
        )

    extract_step["path"] = proposal.path
    extract_step["marker"] = proposal.marker or old_marker
    extract_step["headerContract"] = first.table.header_contract()
    if proposal.query:
        extract_step["query"] = dict(proposal.query)
    schema.setdefault("x-portalc", {})["headers"] = {c.name: c.header for c in first.table.columns}
    # New optional fields are a minor bump; a moved page with the same contract is a patch.
    level = "minor" if live_fields - set((schema.get("items") or {}).get("properties") or {}) else "patch"
    version = bump(_portal_version(ws, portal), level)
    write_json(cfg.flow_path(flow), flow_doc)
    write_json(cfg.schema_path(schema_name), schema)
    lock = load_lock(ws)
    lock.setdefault("portals", {}).setdefault(portal, {})["version"] = version
    from .lockfile import save_lock

    save_lock(ws, lock)

    _confirm_twice(
        ws, portal, flow, params=params, engine_name=engine_name, engine_options=engine_options
    )
    record_green(
        ws,
        portal,
        flow,
        schema_name=schema_name,
        version=version,
        flow_path=cfg.flow_path(flow),
        schema_path=cfg.schema_path(schema_name),
    )
    with (cfg.dir / "CHANGELOG.md").open("a") as fh:
        fh.write(
            f"- {_today()}  repair `{flow}` ({level} {version}): table moved "
            f"{old_path} → {proposal.path} (agent proposal, 2/2 confirmed)\n"
        )
    diff_lines = [f"- path: {old_path}", f"+ path: {proposal.path}"]
    if (proposal.marker or old_marker) != old_marker:
        diff_lines += [f"- marker: {old_marker}", f"+ marker: {proposal.marker}"]
    pr_body = _agent_pr_body(
        portal, flow, failure, old_path, proposal, version, level, engine_used, browser_seconds
    )
    pr_path = ws.state_dir / f"repair-{portal}-{flow}.md"
    pr_path.parent.mkdir(parents=True, exist_ok=True)
    pr_path.write_text(pr_body)
    generate_ts_client(ws, portal, schema, flow, version)
    return {
        "kind": "moved",
        "version": version,
        "diff": diff_lines,
        "prPath": str(pr_path),
        "prBody": pr_body,
        "confirmations": 2,
        "engine": engine_used,
        "agentUsd": proposal.usd,
        "agentNotes": proposal.notes,
        "browserSeconds": browser_seconds,
    }


def _agent_pr_body(
    portal: str,
    flow: str,
    failure: str,
    old_path: str,
    proposal: Any,
    version: str,
    level: str,
    engine: str,
    browser_seconds: float,
) -> str:
    cost = f"${proposal.usd:.2f} agent" if proposal.usd is not None else "agent cost not reported"
    lines = [
        f"portalc: {portal} drift — {flow} reads from a different page",
        "",
        f"- portal: `{portal}`",
        f"- flow: `{flow}`",
        f"- proposed version: `{version}` ({level})",
        "- confirmations: 2/2 green after patch",
        f"- engine: `{engine}`  ·  cost: {cost}, browser {browser_seconds:.0f}s",
        "",
        "What the contract test saw",
        "",
        f"- {failure}",
        "",
        "What the agent found",
        "",
        f"- `{old_path}` → `{proposal.path}`",
    ]
    if proposal.notes:
        lines.append(f"- {proposal.notes}")
    lines += [
        "",
        "How this was checked",
        "",
        "- the agent only proposed a location; the rows were read by the deterministic engine",
        "- the same rows came back twice, and still validate against the committed schema",
        "- `verify` ran twice after the patch and was green both times",
        "",
        "Read the diff before merging. Write-flows never auto-merge.",
        "",
    ]
    return "\n".join(lines) + "\n"


def _pr_body(portal: str, flow: str, old: dict[str, str], new: dict[str, str], version: str, findings: list[Finding]) -> str:
    lines = [
        f"portalc: {portal} drift — column title rename",
        "",
        f"- portal: `{portal}`",
        f"- flow: `{flow}`",
        f"- proposed version: `{version}` (patch — field names unchanged)",
        f"- confirmations: 2/2 green after patch",
        "",
        "What changed",
        "",
    ]
    for k in old:
        if old[k] != new.get(k):
            lines.append(f"- `{k}`: {old[k]!r} → {new.get(k)!r}")
    lines += [
        "",
        "This is a presentation change. Schema field names are stable, so callers keep compiling.",
        "Merge after reading the diff. Write-flows never auto-merge.",
        "",
    ]
    return "\n".join(lines) + "\n"


def build(ws: Workspace, portal: str, flow: str | None = None) -> dict[str, Any]:
    cfg = PortalConfig.load(ws, portal)
    flows = [flow] if flow else _flows_on_disk(cfg)
    if not flows:
        raise UsageError("nothing to build", hint="portalc explore --yes first")
    fl = flows[0]
    flow_doc = read_json(cfg.flow_path(fl), "flow")
    schema = read_json(cfg.schema_path(flow_doc["schema"]), "schema")
    version = _portal_version(ws, portal)
    pkg = generate_ts_client(ws, portal, schema, fl, version)
    return {"package": str(pkg), "version": version, "flow": fl}


def doctor(ws: Workspace) -> list[Finding]:
    findings: list[Finding] = []
    py = sys.version.split()[0]
    findings.append(Finding(check="python", ok=True, message=f"{py} (needs >=3.10)"))
    try:
        import jsonschema  # noqa: F401

        findings.append(Finding(check="jsonschema", ok=True, message="installed"))
    except ImportError:
        findings.append(Finding(check="jsonschema", ok=False, message="missing — pip install -e ."))
    from .chrome import chrome_status

    chrome = chrome_status()
    findings.append(
        Finding(
            check="chrome",
            ok=True,
            severity="info" if chrome["ok"] else "warn",
            message=(
                f"{chrome['version'] or chrome['binary']}  ({chrome['binary']}) — --engine chrome ready"
                if chrome["ok"]
                else "not found (optional; the HTTP engine does not need it)"
            ),
        )
    )
    try:
        import browser_use  # noqa: F401

        findings.append(Finding(check="browser-use", ok=True, message="installed (explore/repair agent extra)"))
    except ImportError:
        findings.append(
            Finding(
                check="browser-use",
                ok=True,
                severity="info",
                message="not installed — optional, only for agent explore/repair",
            )
        )
    node = shutil.which("node")
    findings.append(
        Finding(
            check="node",
            ok=True,
            severity="info" if node else "warn",
            message=node or "not found (only needed to typecheck the generated TS client)",
        )
    )
    demo = Path(demo_server_script()) if _demo_exists() else None
    findings.append(
        Finding(
            check="demo",
            ok=demo is not None,
            message=str(demo) if demo else "demo/portal/server.py missing",
        )
    )
    portals = ws.list_portals()
    findings.append(
        Finding(
            check="workspace",
            ok=True,
            message=f"{ws.root}  portals: {', '.join(portals) or '(none)'}",
        )
    )
    return findings


def _demo_exists() -> bool:
    try:
        demo_server_script()
        return True
    except ConfigError:
        return False


def wait_for_http(url: str, timeout: float = 8.0) -> None:
    import urllib.request

    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as resp:
                if resp.status == 200:
                    return
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(0.1)
    raise PortalAuthError("demo portal did not become reachable", str(last), hint="portalc demo")


def pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def start_demo_process(host: str, port: int) -> subprocess.Popen:
    script = demo_server_script()
    return subprocess.Popen(
        [sys.executable, str(script), "--host", host, "--port", str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
