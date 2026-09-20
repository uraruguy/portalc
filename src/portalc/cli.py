"""portalc CLI — a compiler, not a chatbot."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import typer

from . import __version__
from .envfile import load_workspace_env
from .errors import PortalcError, PortalDownError, PortalDriftError, VerificationFailed
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


def _chrome_options(
    cdp_url: Optional[str] = None,
    headless: bool = False,
    profile: Optional[Path] = None,
) -> dict:
    """Only ever passed to the chrome engine; the HTTP engine takes no options."""
    options: dict = {}
    if cdp_url:
        options["cdp_url"] = cdp_url
    if headless:
        options["headless"] = True
    if profile:
        options["profile_dir"] = profile
    return options


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
    """Scaffold a new portalc workspace. A clone of portalc already has one."""
    ws = Workspace((path or Path.cwd()).resolve())
    banner("init", str(ws.root))
    if not demo:
        raise typer.BadParameter("v0.1 only scaffolds --demo; pass --demo (the default)")
    result = init_demo(ws, base_url=base_url, force=force)
    ok(f"wrote {result['path']}")
    ok("wrote .env.example  (copy to .env — never commit values)")
    if result["gitignoreUpdated"]:
        ok("wrote .gitignore  (.env is ignored, so `git add .` cannot commit credentials)")
    else:
        ok(".gitignore already ignores .env")
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
    port: int = typer.Option(8787, "--port", help="Port to bind. Must match baseUrl in portal.yaml."),
    host: str = typer.Option("127.0.0.1", "--host", help="Interface to bind. Keep the loopback default."),
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
    if _port_listening(host, port):
        alt = port + 1
        raise PortalDownError(
            f"port {port} is already in use",
            f"{host}:{port} already has a process — often a leftover `portalc demo`",
            hint=(
                f"lsof -i :{port}  then kill that PID,  "
                f"or  portalc demo --port {alt}  and set baseUrl in "
                f"portals/demo-marina/portal.yaml to http://{host}:{alt}"
            ),
        )
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
    month: str = typer.Option(DEFAULT_MONTH, "--month", help="Month to compile against, YYYY-MM."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Accept the inferred schema without a prompt."),
    engine: str = typer.Option("http", "--engine", help="http: deterministic HTML, the default. chrome: a real browser over CDP."),
    agent: bool = typer.Option(False, "--agent", help="Let a browser-use agent find the page first (optional extra)."),
    cdp_url: Optional[str] = typer.Option(None, "--cdp-url", help="Attach to a Chrome you already signed into, e.g. http://127.0.0.1:9222."),
    headless: bool = typer.Option(False, "--headless", help="Chrome without a window (no human login possible)."),
    chrome_profile: Optional[Path] = typer.Option(None, "--chrome-profile", help="Chrome user-data-dir to reuse (default ~/.portalc/chrome-profile)."),
) -> None:
    """Walk a portal once and write schema + flow + fixture. The LLM is opt-in (--agent)."""
    ws = _ws()
    banner("explore", f"{portal}/{flow}  ·  engine {engine}")
    if engine == "chrome":
        step("opening Chrome — you sign in, portalc inherits the session")
    else:
        step("signing in (credentials from the environment, never from git)")
    result = explore(
        ws,
        portal,
        flow,
        month=month,
        engine_name=engine,
        engine_options=_chrome_options(cdp_url, headless, chrome_profile),
        use_agent=agent,
        accept=yes or not console().is_terminal,
    )
    schema_table(f"schema  {flow}", result["columns"])
    ok(f"{result['rowCount']} rows  ·  dry-run  {result.get('dryRuns', 2)}/2 identical")
    if result.get("preview"):
        warn("schema not written (re-run with --yes to compile)")
        raise typer.Exit(2)
    if result.get("agentNotes"):
        info(f"agent: {result['agentNotes']}")
    ok(f"wrote {result['flowPath']}")
    ok(f"wrote {result['schemaPath']}")
    ok(f"wrote {result['fixturePath']}")
    if result.get("screenshotPath"):
        ok(f"wrote {result['screenshotPath']}  (evidence)")
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
def build_cmd(portal: str = typer.Argument(DEMO_PORTAL, help="Portal whose client to generate.")) -> None:
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
    flow: Optional[str] = typer.Option(None, "--flow", help="Single flow to check. Default: every compiled flow."),
    all_portals: bool = typer.Option(False, "--all", help="Every compiled portal in the workspace."),
    month: str = typer.Option(DEFAULT_MONTH, "--month", help="Month to check against, YYYY-MM."),
    engine: Optional[str] = typer.Option(None, "--engine", help="Override the engine the flow was compiled with."),
    cdp_url: Optional[str] = typer.Option(None, "--cdp-url", help="Attach to a running Chrome instead of starting one."),
    headless: bool = typer.Option(False, "--headless", help="Run Chrome without a window (CI)."),
    chrome_profile: Optional[Path] = typer.Option(None, "--chrome-profile", help="Chrome user-data-dir holding the signed-in session."),
) -> None:
    """Run contract tests. The model does not get a vote."""
    ws = _ws()
    banner("verify", portal or "all portals")
    try:
        payload = verify(
            ws,
            None if all_portals or portal is None else portal,
            flow,
            params={"month": month},
            engine_name=engine,
            engine_options=_chrome_options(cdp_url, headless, chrome_profile),
        )
    except VerificationFailed as exc:
        if exc.data and exc.data.get("reports"):
            for report in exc.data["reports"]:
                if report["ok"]:
                    ok(f"{report['portal']}/{report['flow']}")
                else:
                    bad(f"{report['portal']}/{report['flow']}")
                    findings_table(report["findings"])
        raise
    engines = sorted({report.get("engine", "http") for report in payload["reports"]})
    for report in payload["reports"]:
        ok(f"{report['portal']}/{report['flow']}  {len(report['rows'])} rows")
        findings_table(report["findings"])
    cost_note(None, 1.0, "+".join(engines) or "http")


@app.command("repair")
def repair_cmd(
    portal: str = typer.Argument(DEMO_PORTAL, help="Portal that drifted."),
    flow: str = typer.Option("list-invoices", "--flow", help="Flow to repair."),
    month: str = typer.Option(DEFAULT_MONTH, "--month", help="Month to re-read, YYYY-MM."),
    engine: Optional[str] = typer.Option(None, "--engine", help="Override the engine the flow was compiled with."),
    agent: bool = typer.Option(False, "--agent", help="If the rename patch does not apply, ask a browser-use agent where the data moved."),
    cdp_url: Optional[str] = typer.Option(None, "--cdp-url", help="Attach to a running Chrome instead of starting one."),
    headless: bool = typer.Option(False, "--headless", help="Run Chrome without a window (CI)."),
    chrome_profile: Optional[Path] = typer.Option(None, "--chrome-profile", help="Chrome user-data-dir holding the signed-in session."),
) -> None:
    """Re-read the live page and write a patch if the drift is a column rename."""
    ws = _ws()
    banner("repair", f"{portal}/{flow}")
    step("re-reading the live page" + ("" if agent else " (no LLM unless you pass --agent)"))
    result = repair(
        ws,
        portal,
        flow,
        params={"month": month},
        engine_name=engine,
        engine_options=_chrome_options(cdp_url, headless, chrome_profile),
        use_agent=agent,
    )
    label = "rename patch" if result["kind"] == "rename" else "moved-page patch"
    ok(f"{label}  →  v{result['version']}  ·  confirmations {result['confirmations']}/2")
    diff_block(result["diff"])
    if result.get("agentNotes"):
        info(f"agent: {result['agentNotes']}")
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
    portal: str = typer.Argument(..., help="Compiled portal name, as in portals/."),
    flow: str = typer.Argument(..., help="Compiled flow name, e.g. list-invoices."),
    set_pairs: list[str] = typer.Option(None, "--set", help="key=value flow params, e.g. month=2026-09"),
    json_out: bool = typer.Option(False, "--json", help="Print rows as JSON (also used by the generated TS client)."),
    engine: Optional[str] = typer.Option(None, "--engine", help="Override the engine the flow was compiled with."),
    cdp_url: Optional[str] = typer.Option(None, "--cdp-url", help="Attach to a running Chrome instead of starting one."),
    headless: bool = typer.Option(False, "--headless", help="Run Chrome without a window."),
    chrome_profile: Optional[Path] = typer.Option(None, "--chrome-profile", help="Chrome user-data-dir holding the signed-in session."),
) -> None:
    """Execute a compiled flow. Refuses to return rows that fail the schema."""
    ws = _ws()
    params: dict[str, str] = {}
    for pair in set_pairs or []:
        if "=" not in pair:
            raise typer.BadParameter(f"--set expects key=value, got {pair!r}")
        k, _, v = pair.partition("=")
        params[k] = v
    result = run_flow(
        ws,
        portal,
        flow,
        params=params,
        validate=True,
        engine_name=engine,
        engine_options=_chrome_options(cdp_url, headless, chrome_profile),
    )
    if not result["ok"]:
        # `run` refusing rows is drift (exit 6), not a failed contract test (exit 8):
        # the generated TS client maps 6 to PortalDriftError.
        raise PortalDriftError(
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
            "portalc demo            start the bundled marina in another terminal",
            "portalc explore --yes   compile list-invoices against it",
            "portalc verify          run the contract test",
        ]
    )


def _port_listening(host: str, port: int) -> bool:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.3)
        return sock.connect_ex((host, port)) == 0


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
