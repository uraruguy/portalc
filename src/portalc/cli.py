"""portalc CLI — a compiler, not a chatbot."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import typer

from . import __version__
from .envfile import load_workspace_env
from .errors import PortalcError, VerificationFailed
from .pipeline import (
    DEFAULT_MONTH,
    DEMO_PORTAL,
    build,
    doctor,
    explore,
    init_demo,
    prepare,
    repair,
    run_flow,
    start_demo_process,
    verify,
    wait_for_http,
)
from .ui import (
    bad,
    banner,
    blank,
    console,
    cost_note,
    diff_block,
    findings_table,
    info,
    next_steps,
    ok,
    print_json,
    render_error,
    schema_table,
    step,
    warn,
)
from .workspace import Workspace

app = typer.Typer(
    name="portalc",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_show_locals=False,
    help="Compile a login-walled portal into a versioned typed SDK, schema, and contract test.",
    epilog="Not a scraper. Not a hosted API. The artifact lives in your git repo.",
)

state = {"debug": False, "json": False}


def _ws() -> Workspace:
    ws = Workspace.discover()
    load_workspace_env(ws.root)
    return prepare(ws)


@app.callback(invoke_without_command=False)
def _root(
    debug: bool = typer.Option(False, "--debug", help="Show Python tracebacks."),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output where applicable."),
    quiet: bool = typer.Option(False, "--quiet", help="Suppress decorative output."),
) -> None:
    state["debug"] = debug
    state["json"] = json_out
    if quiet:
        os.environ["PORTALC_QUIET"] = "1"


@app.command()
def version() -> None:
    """Print the portalc version."""
    console().print(__version__)


@app.command("init")
def init_cmd(
    demo: bool = typer.Option(True, "--demo/--no-demo", help="Scaffold the bundled Demo Marina portal."),
    force: bool = typer.Option(False, "--force", help="Overwrite an existing demo workspace."),
    base_url: str = typer.Option("http://127.0.0.1:8787", "--base-url", help="Demo portal origin."),
    path: Optional[Path] = typer.Option(None, "--path", help="Workspace root (default: cwd)."),
) -> None:
    """Create a portalc workspace. Start here."""
    ws = Workspace((path or Path.cwd()).resolve())
    banner("init", str(ws.root))
    if not demo:
        raise typer.BadParameter("v0.1 only scaffolds --demo; pass --demo (the default)")
    result = init_demo(ws, base_url=base_url, force=force)
    ok(f"wrote {result['path']}")
    ok("wrote .env.example  (copy to .env — never commit values)")
    ok("wrote .github/workflows/portalc.yml")
    blank()
    next_steps(
        [
            "portalc demo            start the bundled marina in another terminal",
            "portalc explore --yes   compile list-invoices against it",
            "portalc verify          run the contract test",
        ]
    )


@app.command("demo")
def demo_cmd(
    port: int = typer.Option(8787, "--port", help="Port to bind."),
    host: str = typer.Option("127.0.0.1", "--host"),
    background: bool = typer.Option(False, "--background", help="Start and exit (tests / scripts)."),
) -> None:
    """Run the bundled Demo Marina partner portal."""
    from .pipeline import demo_server_script
    import subprocess
    import sys

    banner("demo", f"http://{host}:{port}")
    script = demo_server_script()
    info(f"{script}")
    info("sign in as skipper@demo-marina.test / demo-portal-password")
    if background:
        proc = start_demo_process(host, port)
        wait_for_http(f"http://{host}:{port}/healthz")
        ok(f"listening on http://{host}:{port}  pid {proc.pid}")
        return
    raise SystemExit(subprocess.call([sys.executable, str(script), "--host", host, "--port", str(port)]))


@app.command("explore")
def explore_cmd(
    portal: str = typer.Argument(DEMO_PORTAL, help="Portal name under portals/."),
    flow: str = typer.Option("list-invoices", "--flow", help="Flow to compile."),
    month: str = typer.Option(DEFAULT_MONTH, "--month"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Accept the inferred schema without a prompt."),
    engine: str = typer.Option("http", "--engine", help="http (deterministic) or chrome (not in v0.1)."),
) -> None:
    """Walk a portal once and write schema + flow + fixture. No LLM on --engine http."""
    ws = _ws()
    banner("explore", f"{portal}/{flow}")
    step("signing in (credentials from the environment, never from git)")
    result = explore(ws, portal, flow, month=month, engine_name=engine, accept=yes or not console().is_terminal)
    schema_table(f"schema  {flow}", result["columns"])
    ok(f"{result['rowCount']} rows  ·  dry-run  {result.get('dryRuns', 2)}/2 identical")
    if result.get("preview"):
        warn("schema not written (re-run with --yes to compile)")
        raise typer.Exit(2)
    ok(f"wrote {result['flowPath']}")
    ok(f"wrote {result['schemaPath']}")
    ok(f"wrote {result['fixturePath']}")
    cost_note(result.get("agentUsd"), result.get("browserSeconds") or 0, result.get("engine") or "http")
    blank()
    next_steps(
        [
            f"portalc build {portal}",
            f"portalc verify {portal}",
            f"portalc run {portal} {flow} --set month={month}",
        ]
    )


@app.command("build")
def build_cmd(portal: str = typer.Argument(DEMO_PORTAL)) -> None:
    """Generate the typed TypeScript client from the compiled schema."""
    ws = _ws()
    banner("build", portal)
    result = build(ws, portal)
    ok(f"generated {result['package']}  v{result['version']}")
    info("the client shells out to `portalc run` so secrets never enter the package")
    next_steps([f"import {{ listInvoices }} from \"@portalc/portal-{portal}\""])


@app.command("verify")
def verify_cmd(
    portal: Optional[str] = typer.Argument(None, help="Portal to verify. Omit to run --all."),
    flow: Optional[str] = typer.Option(None, "--flow"),
    all_portals: bool = typer.Option(False, "--all", help="Every compiled portal in the workspace."),
    month: str = typer.Option(DEFAULT_MONTH, "--month"),
) -> None:
    """Run contract tests. The model does not get a vote."""
    ws = _ws()
    banner("verify", portal or "all portals")
    try:
        payload = verify(ws, None if all_portals or portal is None else portal, flow, params={"month": month})
    except VerificationFailed as exc:
        if exc.data and exc.data.get("reports"):
            for report in exc.data["reports"]:
                if report["ok"]:
                    ok(f"{report['portal']}/{report['flow']}")
                else:
                    bad(f"{report['portal']}/{report['flow']}")
                    findings_table(report["findings"])
        raise
    for report in payload["reports"]:
        ok(f"{report['portal']}/{report['flow']}  {len(report['rows'])} rows")
        findings_table(report["findings"])
    cost_note(None, 1.0, "http")


@app.command("repair")
def repair_cmd(
    portal: str = typer.Argument(DEMO_PORTAL),
    flow: str = typer.Option("list-invoices", "--flow"),
    month: str = typer.Option(DEFAULT_MONTH, "--month"),
) -> None:
    """Re-read the live page and write a patch if the drift is a column rename."""
    ws = _ws()
    banner("repair", f"{portal}/{flow}")
    step("re-reading the live page (no LLM on --engine http)")
    result = repair(ws, portal, flow, params={"month": month})
    ok(f"rename patch  →  v{result['version']}  ·  confirmations {result['confirmations']}/2")
    diff_block(result["diff"])
    ok(f"PR body  {result['prPath']}")
    cost_note(result.get("agentUsd"), result.get("browserSeconds") or 0, result.get("engine") or "http")
    blank()
    next_steps(
        [
            "read the diff — field names are unchanged, only titles moved",
            f"git add portals/{portal} generated portalc.lock && git commit",
            "write-flows never auto-merge; this one is read-only",
        ]
    )


@app.command("run")
def run_cmd(
    portal: str = typer.Argument(...),
    flow: str = typer.Argument(...),
    set_pairs: list[str] = typer.Option(None, "--set", help="key=value flow params, e.g. month=2026-09"),
    json_out: bool = typer.Option(False, "--json", help="Print rows as JSON (also used by the generated TS client)."),
) -> None:
    """Execute a compiled flow. Refuses to return rows that fail the schema."""
    ws = _ws()
    params: dict[str, str] = {}
    for pair in set_pairs or []:
        if "=" not in pair:
            raise typer.BadParameter(f"--set expects key=value, got {pair!r}")
        k, _, v = pair.partition("=")
        params[k] = v
    result = run_flow(ws, portal, flow, params=params, validate=True)
    if not result["ok"]:
        raise VerificationFailed(
            next(f.message for f in result["findings"] if not f.ok),
            [f.message for f in result["findings"] if not f.ok],
            data={"findings": [f.as_dict() for f in result["findings"]]},
        )
    if json_out or state["json"]:
        print_json(result["rows"])
        return
    banner("run", f"{portal}/{flow}")
    ok(f"{len(result['rows'])} rows")
    findings_table(result["findings"])


@app.command("doctor")
def doctor_cmd() -> None:
    """Check whether this machine can run portalc."""
    ws = _ws()
    banner("doctor", str(ws.root))
    findings_table(doctor(ws))
    next_steps(
        [
            "portalc init --demo",
            "portalc demo",
            "portalc explore --yes && portalc verify",
        ]
    )


def main() -> int:
    try:
        app()
        return 0
    except PortalcError as exc:
        return render_error(exc, debug=state["debug"])
    except Exception as exc:  # noqa: BLE001
        return render_error(exc, debug=True)


if __name__ == "__main__":
    raise SystemExit(main())
