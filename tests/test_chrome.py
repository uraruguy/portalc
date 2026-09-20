"""Chrome engine and agent seam.

The interesting half of this file needs no browser: `FakeChrome` is a CDP channel
that serves two pages in memory, so navigation, form sign-in, the login-wall
check and status mapping are covered on a machine with no display. The tests that
do want a real browser skip themselves when Chrome is not installed.
"""

from __future__ import annotations

import base64
import json
import re
import shutil
from pathlib import Path
from typing import Any

import pytest

from portalc import agent as agent_mod
from portalc.chrome import ChromeEngine, ChromeOptions, chrome_status, find_chrome
from portalc.errors import (
    BrowserUnavailableError,
    PortalAuthError,
    PortalBlockedError,
    PortalDriftError,
    UsageError,
)
from portalc.pipeline import explore, repair, run_flow, verify
from portalc.workspace import Workspace, read_json, write_json

USER = "skipper@demo-marina.test"
PASSWORD = "demo-portal-password"

LOGIN_HTML = """<!doctype html><html><body>
<form class="login" method="post" action="/login" data-portalc="login-form">
  <input id="username" name="username" type="email">
  <input id="password" name="password" type="password">
  <button type="submit" data-portalc="login-submit">Sign in</button>
</form></body></html>"""

INVOICES_HTML = """<!doctype html><html><body>
<table data-portalc="invoices">
<thead><tr>
  <th data-col="number">Invoice no.</th>
  <th data-col="issued_on">Issued</th>
  <th data-col="total_eur">Total (EUR)</th>
  <th data-col="currency">Currency</th>
</tr></thead>
<tbody>
  <tr><td>2026-000412</td><td>2026-09-02</td><td>1840.00</td><td>EUR</td></tr>
  <tr><td>2026-000413</td><td>2026-09-11</td><td>420.50</td><td>EUR</td></tr>
</tbody></table></body></html>"""

BLOCKED_HTML = "<!doctype html><html><body><h1>403</h1></body></html>"

_SELECTOR_ATTRS = re.compile(r'\[?([a-z-]+)=("([^"]*)"|\'([^\']*)\')\]?')
_QUERY_SELECTOR = re.compile(r'document\.querySelector\((".*?")\)')


class FakeChrome:
    """A `CdpChannel` serving a two-page portal, including its login wall."""

    def __init__(self) -> None:
        self.url = "about:blank"
        self.html = "<html></html>"
        self.status = 200
        self.authenticated = False
        self.typed: dict[str, str] = {}
        self.focused: str | None = None
        self.submits = 0
        self.screenshots = 0
        self.closed = False
        self._events: list[dict[str, Any]] = []

    # -- CdpChannel ------------------------------------------------------
    def send(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        params = params or {}
        if method in {"Page.enable", "Network.enable"}:
            return {}
        if method == "Page.navigate":
            self._navigate(params["url"])
            return {"frameId": "1"}
        if method == "Runtime.evaluate":
            return {"result": {"value": self._evaluate(params["expression"])}}
        if method == "Input.insertText":
            if self.focused:
                self.typed[self.focused] = self.typed.get(self.focused, "") + params["text"]
            return {}
        if method == "Page.captureScreenshot":
            self.screenshots += 1
            return {"data": base64.b64encode(b"fake-png").decode()}
        raise AssertionError(f"FakeChrome got an unexpected CDP call: {method}")

    def take_events(self, method: str) -> list[dict[str, Any]]:
        keep, taken = [], []
        for event in self._events:
            (taken if event.get("method") == method else keep).append(event)
        self._events = keep
        return taken

    def close(self) -> None:
        self.closed = True

    # -- portal ----------------------------------------------------------
    def _navigate(self, url: str) -> None:
        path = url.split("://", 1)[-1].split("/", 1)[-1]
        path = "/" + path.split("?", 1)[0].lstrip("/")
        if path == "/blocked":
            self._serve(url, BLOCKED_HTML, 403)
        elif path.startswith("/invoices") and self.authenticated:
            self._serve(url, INVOICES_HTML, 200)
        elif path.startswith("/invoices"):
            self._serve("http://portal.test/login", LOGIN_HTML, 200)
        else:
            self._serve(url, LOGIN_HTML, 200)

    def _serve(self, url: str, html: str, status: int) -> None:
        self.url = url
        self.html = html
        self.status = status
        self._events.append(
            {
                "method": "Network.responseReceived",
                "params": {"type": "Document", "response": {"status": status, "url": url}},
            }
        )

    def _matches(self, selector: str) -> bool:
        for part in selector.split(","):
            attrs = _SELECTOR_ATTRS.findall(part.strip())
            if not attrs:
                continue
            if all(f'{name}="{quoted[1:-1]}"' in self.html for name, quoted, *_ in attrs):
                return True
        return False

    def _evaluate(self, expression: str) -> Any:
        if expression == "location.href":
            return self.url
        if expression == "document.readyState":
            return "complete"
        if expression == "document.documentElement.outerHTML":
            return self.html
        selector_match = _QUERY_SELECTOR.search(expression)
        selector = json.loads(selector_match.group(1)) if selector_match else ""
        if expression.startswith("!!document.querySelector"):
            return self._matches(selector)
        if "el.focus()" in expression:
            if not self._matches(selector):
                return False
            self.focused = "password" if "password" in selector else "username"
            self.typed[self.focused] = ""
            return True
        if "b.click()" in expression:
            self.submits += 1
            if self.typed.get("username") == USER and self.typed.get("password") == PASSWORD:
                self.authenticated = True
                self._serve("http://portal.test/invoices", INVOICES_HTML, 200)
            else:
                self._serve("http://portal.test/login", LOGIN_HTML, 401)
            return True
        raise AssertionError(f"FakeChrome cannot evaluate: {expression}")


@pytest.fixture
def fake_engine() -> ChromeEngine:
    return ChromeEngine("http://portal.test", FakeChrome(), timeout=2.0)


# -- mocked CDP ----------------------------------------------------------
def test_login_wall_is_an_auth_error_not_a_drift_error(fake_engine: ChromeEngine):
    with pytest.raises(PortalAuthError):
        fake_engine.fetch_table("/invoices", marker="invoices")


def test_form_login_then_extract_over_cdp(fake_engine: ChromeEngine):
    fake_engine.login(USER, PASSWORD, path="/login")
    result = fake_engine.fetch_table("/invoices", query={"month": "2026-09"}, marker="invoices")

    assert result.status == 200
    assert result.table is not None
    assert result.table.field_names() == ["number", "issued_on", "total_eur", "currency"]
    assert result.table.rows[0]["total_eur"] == 1840.00
    assert result.table.header_contract()[2] == "Total (EUR)"


def test_password_is_typed_at_the_input_layer_not_into_javascript(fake_engine: ChromeEngine):
    channel: FakeChrome = fake_engine.cdp  # type: ignore[assignment]
    fake_engine.login(USER, PASSWORD, path="/login")

    assert channel.typed == {"username": USER, "password": PASSWORD}
    assert channel.submits == 1


def test_wrong_credentials_raise_auth_error(fake_engine: ChromeEngine):
    with pytest.raises(PortalAuthError):
        fake_engine.login(USER, "wrong-password", path="/login")


def test_http_403_is_a_blocked_error(fake_engine: ChromeEngine):
    with pytest.raises(PortalBlockedError):
        fake_engine.get("/blocked")


def test_human_login_waits_for_the_human(fake_engine: ChromeEngine):
    channel: FakeChrome = fake_engine.cdp  # type: ignore[assignment]
    announced: list[str] = []

    def human_signs_in() -> None:
        channel.authenticated = True
        channel._serve("http://portal.test/invoices", INVOICES_HTML, 200)

    fake_engine.await_human_login(login_path="/login", wait=human_signs_in, announce=announced.append)
    assert channel.authenticated
    assert announced

    table = fake_engine.fetch_table("/invoices", marker="invoices").table
    assert table is not None and len(table.rows) == 2


def test_human_login_that_nobody_answers_is_an_auth_error(fake_engine: ChromeEngine):
    with pytest.raises(PortalAuthError):
        fake_engine.await_human_login(login_path="/login", wait=lambda: None, announce=lambda _m: None)


def test_screenshot_and_close(fake_engine: ChromeEngine):
    assert fake_engine.screenshot_png() == b"fake-png"
    fake_engine.close()
    assert fake_engine.cdp.closed  # type: ignore[attr-defined]


def test_chrome_options_read_the_environment(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PORTALC_CDP_URL", "http://127.0.0.1:9222")
    monkeypatch.setenv("PORTALC_CHROME_HEADLESS", "1")
    monkeypatch.setenv("PORTALC_CHROME_TIMEOUT", "45")

    opts = ChromeOptions.from_env()
    assert opts.cdp_url == "http://127.0.0.1:9222"
    assert opts.headless is True
    assert opts.timeout == 45.0

    overridden = ChromeOptions.from_env(headless=False, profile_dir="/tmp/profile")
    assert overridden.headless is False
    assert overridden.profile_dir == Path("/tmp/profile")


def test_missing_chrome_binary_names_the_way_out(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("PORTALC_CHROME_BINARY", raising=False)
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    with pytest.raises(BrowserUnavailableError) as caught:
        find_chrome()
    assert "--engine chrome" in caught.value.hint


# -- the agent seam ------------------------------------------------------
def test_agent_is_optional_and_says_how_to_install_it(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(agent_mod, "browser_use_available", lambda: False)
    with pytest.raises(BrowserUnavailableError) as caught:
        agent_mod.require_browser_use()
    assert "[agent]" in caught.value.hint


def test_agent_may_not_leave_the_allowed_domains():
    with pytest.raises(UsageError):
        agent_mod.propose(
            "find the invoices",
            base_url="https://someone-elses-portal.example",
            allowed_domains=["127.0.0.1", "localhost"],
            runner=lambda _task, _ctx: {"path": "/invoices"},
        )


def test_agent_task_never_contains_a_credential():
    task = agent_mod.explore_task("http://127.0.0.1:8787", "list-invoices", {"month": "2026-09"})
    assert PASSWORD not in task and USER not in task
    assert "never type or ask for credentials" in task


def test_explore_with_an_agent_proposal_is_still_confirmed_deterministically(workspace: Path):
    ws = Workspace(workspace)
    seen: list[str] = []

    def stub_runner(task: str, ctx: dict[str, Any]) -> dict[str, Any]:
        seen.append(task)
        return {
            "path": "/invoices",
            "marker": "invoices",
            "query": {"month": "2026-09"},
            "notes": "Invoices are still on /invoices.",
            "usd": 0.14,
        }

    result = explore(
        ws,
        "demo-marina",
        "list-invoices",
        path="/nowhere",  # the agent's proposal is what gets used
        use_agent=True,
        agent_runner=stub_runner,
        accept=True,
    )

    assert seen, "the agent should have been asked exactly once"
    assert result["dryRuns"] == 2
    assert result["agentUsd"] == 0.14
    flow = read_json(Path(result["flowPath"]), "flow")
    assert flow["steps"][1]["path"] == "/invoices"
    assert flow["compiledBy"] == {"agent": True, "notes": "Invoices are still on /invoices."}
    assert verify(ws, "demo-marina", "list-invoices")["ok"]


def test_agent_repair_moves_the_flow_and_confirms_twice(workspace: Path):
    ws = Workspace(workspace)
    explore(ws, "demo-marina", "list-invoices", accept=True)

    flow_path = workspace / "portals" / "demo-marina" / "flows" / "list-invoices.flow.json"
    flow = read_json(flow_path, "flow")
    flow["steps"][1]["path"] = "/statements"  # the portal "moved" the page
    write_json(flow_path, flow)

    result = repair(
        ws,
        "demo-marina",
        "list-invoices",
        use_agent=True,
        agent_runner=lambda _task, _ctx: {
            "path": "/invoices",
            "marker": "invoices",
            "notes": "The invoice list moved back to /invoices.",
            "usd": 0.21,
        },
    )

    assert result["kind"] == "moved"
    assert result["confirmations"] == 2
    assert read_json(flow_path, "flow")["steps"][1]["path"] == "/invoices"
    assert "deterministic engine" in result["prBody"]
    assert verify(ws, "demo-marina", "list-invoices")["ok"]


def test_agent_proposal_that_breaks_the_contract_is_refused(workspace: Path):
    ws = Workspace(workspace)
    explore(ws, "demo-marina", "list-invoices", accept=True)

    with pytest.raises(PortalDriftError) as caught:
        repair(
            ws,
            "demo-marina",
            "list-invoices",
            use_agent=True,
            agent_runner=lambda _task, _ctx: {"path": "/berths", "marker": "berths", "notes": "wrong table"},
        )
    assert "does not satisfy the committed schema" in caught.value.title


# -- a real browser ------------------------------------------------------
chrome_missing = not chrome_status()["ok"]
needs_chrome = pytest.mark.skipif(chrome_missing, reason="no Chrome or Chromium on this machine")


@needs_chrome
def test_explore_and_verify_against_a_real_chrome(workspace: Path, tmp_path: Path):
    ws = Workspace(workspace)
    options = {"headless": True, "profile_dir": tmp_path / "chrome-profile"}

    result = explore(
        ws,
        "demo-marina",
        "list-invoices",
        engine_name="chrome",
        engine_options=options,
        accept=True,
    )

    assert result["engine"] == "chrome"
    assert result["rowCount"] >= 6
    assert result["dryRuns"] == 2
    assert "total_eur" in [c["name"] for c in result["columns"]]
    assert Path(result["screenshotPath"]).stat().st_size > 1000

    flow = read_json(Path(result["flowPath"]), "flow")
    assert flow["engine"] == "chrome"

    # the flow remembers chrome, so verify replays it in the browser …
    assert verify(ws, "demo-marina", "list-invoices", engine_options=options)["ok"]
    # … and the artifact is engine-agnostic: the HTTP engine validates it too.
    assert verify(ws, "demo-marina", "list-invoices", engine_name="http")["ok"]

    rows = run_flow(
        ws,
        "demo-marina",
        "list-invoices",
        params={"month": "2026-09"},
        engine_options=options,
    )
    assert rows["ok"] and rows["engine"] == "chrome"
    assert rows["rows"][0]["currency"] == "EUR"


@needs_chrome
def test_chrome_engine_reports_the_login_wall(workspace: Path, demo_portal: str, tmp_path: Path):
    engine = ChromeEngine.start(
        demo_portal, headless=True, profile_dir=tmp_path / "anonymous-profile"
    )
    try:
        with pytest.raises(PortalAuthError):
            engine.fetch_table("/invoices", query={"month": "2026-09"}, marker="invoices")
        engine.login(USER, PASSWORD, path="/login")
        table = engine.fetch_table("/invoices", query={"month": "2026-09"}, marker="invoices").table
        assert table is not None and len(table.rows) >= 6
        assert engine.browser_seconds > 0
    finally:
        engine.close()
