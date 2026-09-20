"""The CLI surface: what a user types, what they get back, and what the shell sees.

Exit codes are part of the contract — the generated TypeScript client branches on
them — so every failure mode is pinned here rather than only at library level.
"""

from __future__ import annotations

import json
import os
import signal
from pathlib import Path

import pytest

from portalc import __version__
from portalc.workspace import Workspace

COMMANDS = ["version", "init", "demo", "explore", "build", "verify", "repair", "run", "doctor"]


# -- the surface ---------------------------------------------------------


def test_version_prints_the_package_version(cli, tmp_path: Path):
    result = cli("version", cwd=tmp_path)
    assert result.code == 0
    assert result.stdout.strip() == __version__


def test_help_lists_every_documented_command(cli, tmp_path: Path):
    result = cli("--help", cwd=tmp_path)
    assert result.code == 0
    for command in COMMANDS:
        assert command in result.stdout


def test_running_portalc_with_no_command_shows_the_help_and_fails(cli, tmp_path: Path):
    result = cli(cwd=tmp_path)
    assert result.code == 2
    assert "Usage" in result.output


def test_an_unknown_command_is_refused(cli, tmp_path: Path):
    result = cli("frobnicate", cwd=tmp_path)
    assert result.code == 2
    assert "No such command" in result.output


def test_doctor_runs_before_anything_is_configured(cli, tmp_path: Path):
    result = cli("doctor", cwd=tmp_path)
    assert result.code == 0
    for check in ("python", "jsonschema", "demo", "workspace"):
        assert check in result.stdout


def test_quiet_suppresses_the_decoration(cli, tmp_path: Path):
    assert cli("--quiet", "doctor", cwd=tmp_path).stdout == ""
    assert cli("doctor", cwd=tmp_path).stdout != ""


def test_debug_adds_the_traceback_to_an_ordinary_error(cli, tmp_path: Path):
    plain = cli("verify", cwd=tmp_path)
    debugged = cli("--debug", "verify", cwd=tmp_path)
    assert plain.code == debugged.code == 2
    assert "UsageError" not in plain.output
    assert "UsageError" in debugged.output


# -- init ----------------------------------------------------------------


def test_init_scaffolds_a_workspace_a_human_can_read(cli, tmp_path: Path):
    result = cli("init", "--demo", "--base-url", "http://127.0.0.1:8787", cwd=tmp_path)
    assert result.code == 0
    assert (tmp_path / "portals" / "demo-marina" / "portal.yaml").is_file()
    assert (tmp_path / ".env.example").is_file()
    assert (tmp_path / ".github" / "workflows" / "portalc.yml").is_file()
    assert (tmp_path / "portals" / "demo-marina" / "CHANGELOG.md").is_file()
    assert Workspace(tmp_path).list_portals() == ["demo-marina"]


def test_init_refuses_to_overwrite_a_workspace_unless_forced(cli, tmp_path: Path):
    assert cli("init", "--demo", cwd=tmp_path).code == 0
    again = cli("init", "--demo", cwd=tmp_path)
    assert again.code == 2
    assert "already exists" in again.output
    assert "--force" in again.output
    assert cli("init", "--demo", "--force", cwd=tmp_path).code == 0


def test_init_without_demo_is_not_supported_in_v0_1(cli, tmp_path: Path):
    result = cli("init", "--no-demo", cwd=tmp_path)
    assert result.code == 2
    assert "--demo" in result.output


def test_the_scaffolded_env_example_holds_names_and_a_demo_pair_only(cli, tmp_path: Path):
    cli("init", "--demo", cwd=tmp_path)
    body = (tmp_path / ".env.example").read_text()
    assert "DEMO_MARINA_USER" in body and "DEMO_MARINA_PASS" in body
    assert "never the values" in body


# -- the compiled workspace ----------------------------------------------


def test_explore_compiles_the_demo_flow(cli, workspace: Path):
    result = cli("explore", "--yes", cwd=workspace)
    assert result.code == 0
    assert "2/2 identical" in result.stdout
    assert (workspace / "portals/demo-marina/schemas/invoice.schema.json").is_file()


def test_explore_without_yes_previews_and_refuses_to_compile(cli, workspace: Path):
    result = cli("explore", cwd=workspace)
    assert result.code == 2
    assert "re-run with --yes" in result.output
    assert not (workspace / "portals/demo-marina/schemas/invoice.schema.json").exists()


def test_verify_is_green_on_a_freshly_compiled_flow(cli, compiled: Workspace):
    result = cli("verify", cwd=compiled.root)
    assert result.code == 0
    assert "demo-marina/list-invoices" in result.stdout
    assert "column headers match the contract" in result.stdout


def test_build_writes_the_typed_client(cli, compiled: Workspace):
    result = cli("build", "demo-marina", cwd=compiled.root)
    assert result.code == 0
    assert (compiled.root / "generated/ts/portal-demo-marina/index.ts").is_file()


def test_run_json_prints_rows_and_nothing_else_on_stdout(cli, compiled: Workspace):
    result = cli("run", "demo-marina", "list-invoices", "--json", cwd=compiled.root)
    assert result.code == 0
    rows = result.json()
    assert len(rows) >= 6
    assert rows[0]["currency"] == "EUR"
    assert isinstance(rows[0]["total_eur"], float)


def test_run_takes_flow_params_from_set(cli, compiled: Workspace):
    rows = cli("run", "demo-marina", "list-invoices", "--json", "--set", "month=2026-10", cwd=compiled.root).json()
    assert all(r["issued_on"].startswith("2026-10") for r in rows)


def test_set_must_look_like_a_key_value_pair(cli, compiled: Workspace):
    result = cli("run", "demo-marina", "list-invoices", "--set", "month", cwd=compiled.root)
    assert result.code == 2
    assert "key=value" in result.output


def test_run_without_json_prints_a_human_summary(cli, compiled: Workspace):
    result = cli("run", "demo-marina", "list-invoices", cwd=compiled.root)
    assert result.code == 0
    assert "rows" in result.stdout
    assert "schema" in result.stdout


# -- failure modes, by exit code -----------------------------------------


def test_a_workspace_with_no_portals_exits_2(cli, tmp_path: Path):
    (tmp_path / "portalc.lock").write_text("{}")
    result = cli("verify", cwd=tmp_path)
    assert result.code == 2
    assert "no portals in this workspace" in result.output
    assert "portalc init --demo" in result.output


def test_an_unconfigured_portal_exits_2_and_says_what_exists(cli, compiled: Workspace):
    result = cli("run", "other-marina", "list-invoices", cwd=compiled.root)
    assert result.code == 2
    assert "demo-marina" in result.output


def test_wrong_credentials_exit_3(cli, compiled: Workspace):
    result = cli("verify", cwd=compiled.root, env={"DEMO_MARINA_PASS": "not-the-password"})
    assert result.code == 3
    assert "sign-in was rejected" in result.output


def test_an_unreachable_portal_exits_5(cli, tmp_path: Path):
    cli("init", "--demo", "--base-url", "http://127.0.0.1:1", cwd=tmp_path)
    result = cli("explore", "--yes", cwd=tmp_path)
    assert result.code == 5
    assert "not reachable" in result.output
    assert "portalc demo" in result.output


def test_drift_fails_verify_with_exit_8(cli, compiled: Workspace, mutate):
    mutate("rename-total")
    result = cli("verify", cwd=compiled.root)
    assert result.code == 8
    assert "total_eur" in result.output
    assert "Amount (EUR)" in result.output
    assert "portalc repair" in result.output


def test_drift_stops_run_from_returning_rows(cli, compiled: Workspace, mutate):
    mutate("rename-total")
    result = cli("run", "demo-marina", "list-invoices", "--json", cwd=compiled.root)
    # Drift out of `run` is exit 6, the code README and the generated client
    # document; `verify` keeps 8.
    assert result.code == 6
    assert result.stdout == ""
    assert "total_eur" in result.stderr


def test_an_unknown_engine_is_a_usage_error(cli, compiled: Workspace):
    result = cli("explore", "--engine", "banana", "--yes", cwd=compiled.root)
    assert result.code == 2
    assert "unknown engine" in result.output
    assert "chrome" in result.output


def test_repair_refuses_drift_it_cannot_prove_is_a_rename(cli, compiled: Workspace):
    path = compiled.root / "portals/demo-marina/schemas/invoice.schema.json"
    schema = json.loads(path.read_text())
    schema["x-portalc"]["headers"]["mooring_ref"] = "Mooring ref"
    path.write_text(json.dumps(schema, indent=2))

    result = cli("repair", "demo-marina", cwd=compiled.root)
    assert result.code == 6
    assert "cannot auto-patch" in result.output
    assert "portalc explore" in result.output


# -- the bundled server --------------------------------------------------


def test_demo_background_starts_a_portal_and_reports_its_pid(cli, tmp_path: Path):
    import urllib.request

    from portalc.pipeline import pick_free_port

    port = pick_free_port()
    result = cli("demo", "--background", "--port", str(port), cwd=tmp_path)
    assert result.code == 0
    pid = int(result.stdout.rsplit("pid", 1)[1].strip())
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=3) as response:
            assert json.loads(response.read()) == {"ok": True}
    finally:
        os.kill(pid, signal.SIGTERM)
