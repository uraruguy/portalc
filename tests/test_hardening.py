"""Hardening: where portalc may go, what it may say, and what it may write.

Every test here pins a guarantee that is cheap to lose by accident:

* a request may only reach a host on `policy.allowedDomains` — including after
  a redirect, and including a URL that arrived inside a flow file;
* one timeout, bounded retries, and no retry on a POST;
* a failure names the status and never quotes a credential;
* an artifact that would contain a credential value is not written;
* the lockfile can be checked against itself and against the files it locks.
"""

from __future__ import annotations

import contextlib
import json
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from portalc.engine import HttpEngine, dump_evidence
from portalc.errors import ConfigError, PortalAuthError, PortalBlockedError, PortalDownError
from portalc.lockfile import (
    INTEGRITY_FIELD,
    LockIntegrityError,
    assert_integrity,
    integrity_findings,
    load_lock,
    record_green,
    save_lock,
)
from portalc.policy import (
    SECRETS,
    HostPattern,
    PolicyViolation,
    RequestPolicy,
    SecretLeakError,
    parse_allowlist,
    safe_url,
)
from portalc.schemautil import content_fingerprint
from portalc.workspace import PortalConfig, Workspace, write_json

SECRET = "sup3r-secret-password"


@pytest.fixture(autouse=True)
def clean_registry() -> Iterator[None]:
    """The registry is process-wide by design; keep it per-test in here."""
    SECRETS.clear()
    yield
    SECRETS.clear()


# -- a scripted portal --------------------------------------------------------


class Recorder:
    def __init__(self) -> None:
        self.hits: list[tuple[str, str]] = []

    def count(self, method: str = "") -> int:
        return len([h for h in self.hits if not method or h[0] == method])


def reply(handler: BaseHTTPRequestHandler, status: int, body: bytes = b"ok", headers: dict | None = None) -> None:
    handler.send_response(status)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    for key, value in (headers or {}).items():
        handler.send_header(key, value)
    handler.end_headers()
    if body:
        handler.wfile.write(body)


@contextlib.contextmanager
def serving(respond) -> Iterator[tuple[str, Recorder]]:
    """Run a one-off portal that answers however the test tells it to."""
    recorder = Recorder()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def _handle(self) -> None:
            recorder.hits.append((self.command, self.path))
            respond(self, recorder)

        do_GET = _handle
        do_POST = _handle

        def log_message(self, *args) -> None:  # noqa: ANN002
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", recorder
    finally:
        server.shutdown()
        server.server_close()


# -- allowed domains ----------------------------------------------------------


def test_allowlist_defaults_to_the_portal_origin():
    policy = RequestPolicy.for_base_url("https://partner.example.com/portal")
    assert policy.allows("https://partner.example.com/invoices")
    assert not policy.allows("https://elsewhere.example.com/invoices")
    assert not policy.allows("https://evil-partner.example.com.attacker.test/")


def test_wildcard_entries_do_not_match_the_bare_domain():
    pattern = HostPattern.parse("*.example.com")
    assert pattern.matches("eu.example.com", None)
    assert not pattern.matches("example.com", None)
    assert not pattern.matches("example.com.attacker.test", None)


def test_an_entry_with_a_port_pins_that_port():
    allowlist = parse_allowlist(["127.0.0.1:8787"], base_url="http://127.0.0.1:8787")
    policy = RequestPolicy(allowed_domains=allowlist)
    assert policy.allows("http://127.0.0.1:8787/invoices")
    assert not policy.allows("http://127.0.0.1:9999/invoices")


def test_base_url_left_off_the_allowlist_is_a_config_error():
    with pytest.raises(ConfigError) as caught:
        parse_allowlist(["other.example.com"], base_url="https://partner.example.com")
    assert "allowedDomains" in caught.value.title


def test_an_absolute_url_off_the_allowlist_is_refused_before_any_request():
    with serving(lambda h, r: reply(h, 200)) as (base, recorder):
        engine = HttpEngine(base)
        with pytest.raises(PolicyViolation) as caught:
            engine.get("https://invoices.attacker.test/export")
        assert "invoices.attacker.test" in caught.value.title
        assert recorder.count() == 0


def test_non_http_schemes_are_refused():
    with serving(lambda h, r: reply(h, 200)) as (base, _):
        engine = HttpEngine(base)
        with pytest.raises(PolicyViolation):
            engine.get("file:///etc/passwd")


def test_credentials_in_the_url_are_refused():
    with pytest.raises(PolicyViolation) as caught:
        HttpEngine("http://user:hunter2@127.0.0.1:9/portal")
    assert "hunter2" not in str(caught.value.as_dict())


def test_a_redirect_off_the_allowlist_is_refused():
    def respond(handler, _recorder):
        reply(handler, 302, b"", {"Location": "http://accounts.attacker.test/authorize"})

    with serving(respond) as (base, recorder):
        engine = HttpEngine(base)
        with pytest.raises(PolicyViolation) as caught:
            engine.get("/invoices")
        assert "accounts.attacker.test" in caught.value.title
        assert recorder.count() == 1  # the redirect was never followed


def test_a_same_host_redirect_is_followed():
    def respond(handler, _recorder):
        if handler.path == "/invoices":
            reply(handler, 302, b"", {"Location": "/invoices/2026-09"})
        else:
            reply(handler, 200, b"<table>done</table>")

    with serving(respond) as (base, _):
        result = HttpEngine(base).get("/invoices")
        assert result.status == 200
        assert "done" in result.html


def test_a_read_only_portal_refuses_a_post_that_is_not_the_login():
    with serving(lambda h, r: reply(h, 200)) as (base, recorder):
        engine = HttpEngine(base, policy=RequestPolicy.for_base_url(base, read_only=True))
        with pytest.raises(PolicyViolation) as caught:
            engine.post_form("/invoices/42/pay", {"amount": "100"})
        assert "readOnly" in caught.value.title
        assert recorder.count() == 0


# -- timeouts and retries -----------------------------------------------------


def test_a_slow_portal_times_out_and_the_timeout_is_named():
    def respond(handler, _recorder):
        time.sleep(1.0)
        reply(handler, 200)

    with serving(respond) as (base, _):
        engine = HttpEngine(base, policy=RequestPolicy.for_base_url(base, timeout=0.2, retries=0))
        with pytest.raises(PortalDownError) as caught:
            engine.get("/invoices")
        assert "0.2s" in caught.value.title


def test_a_transient_failure_is_retried_then_succeeds():
    def respond(handler, recorder):
        if recorder.count("GET") < 3:
            reply(handler, 502, b"bad gateway")
        else:
            reply(handler, 200, b"<table>ok</table>")

    with serving(respond) as (base, recorder):
        engine = HttpEngine(base, policy=RequestPolicy.for_base_url(base, retries=2, backoff=0.01))
        result = engine.get("/invoices")
        assert result.status == 200
        assert result.attempts == 3
        assert recorder.count("GET") == 3


def test_retries_are_bounded_by_policy():
    with serving(lambda h, r: reply(h, 503, b"down")) as (base, recorder):
        engine = HttpEngine(base, policy=RequestPolicy.for_base_url(base, retries=2, backoff=0.01))
        with pytest.raises(PortalDownError) as caught:
            engine.get("/invoices")
        assert recorder.count("GET") == 3
        assert caught.value.data["attempts"] == 3
        assert caught.value.data["status"] == 503


def test_a_post_is_never_retried():
    with serving(lambda h, r: reply(h, 503, b"down")) as (base, recorder):
        engine = HttpEngine(base, policy=RequestPolicy.for_base_url(base, retries=3, backoff=0.01))
        with pytest.raises(PortalDownError):
            engine.post_form("/login", {"username": "a", "password": SECRET})
        assert recorder.count("POST") == 1


def test_rate_limiting_is_reported_as_blocked_not_as_drift():
    with serving(lambda h, r: reply(h, 429, b"slow down", {"Retry-After": "0"})) as (base, recorder):
        engine = HttpEngine(base, policy=RequestPolicy.for_base_url(base, retries=1, backoff=0.01))
        with pytest.raises(PortalBlockedError) as caught:
            engine.get("/invoices")
        assert "429" in caught.value.title
        assert recorder.count("GET") == 2


def test_an_oversized_response_is_refused():
    with serving(lambda h, r: reply(h, 200, b"x" * 5000)) as (base, _):
        engine = HttpEngine(base, policy=RequestPolicy.for_base_url(base, max_response_bytes=1024))
        with pytest.raises(PolicyViolation) as caught:
            engine.get("/invoices")
        assert "maxResponseBytes" in caught.value.title


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, PortalAuthError),
        (403, PortalBlockedError),
        (404, PortalDownError),
    ],
)
def test_each_status_maps_to_a_named_failure(status: int, expected: type[Exception]):
    with serving(lambda h, r: reply(h, status, b"nope")) as (base, _):
        engine = HttpEngine(base, policy=RequestPolicy.for_base_url(base, retries=0))
        with pytest.raises(expected) as caught:
            engine.get("/invoices")
        assert str(status) in caught.value.title
        assert caught.value.hint


# -- no secrets in anything a human or a file sees ----------------------------


def test_a_failure_does_not_quote_a_credential_from_the_url():
    SECRETS.register(SECRET)
    with serving(lambda h, r: reply(h, 403, b"denied")) as (base, _):
        engine = HttpEngine(base, policy=RequestPolicy.for_base_url(base, retries=0))
        with pytest.raises(PortalBlockedError) as caught:
            engine.get("/invoices", {"token": SECRET})
        rendered = json.dumps(caught.value.as_dict())
        assert SECRET not in rendered
        assert "***" in rendered


def test_login_registers_the_credentials_it_was_given():
    def respond(handler, _recorder):
        length = int(handler.headers.get("Content-Length", "0"))
        body = handler.rfile.read(length).decode()
        # A portal that echoes the submitted form back into its error page.
        reply(handler, 401, f'<div data-portalc="login-error">{body}</div>'.encode())

    with serving(respond) as (base, _):
        engine = HttpEngine(base, policy=RequestPolicy.for_base_url(base, retries=0))
        with pytest.raises(PortalAuthError) as caught:
            engine.login("skipper@example.test", SECRET, path="/login")
        assert SECRET not in json.dumps(caught.value.as_dict())


def test_safe_url_masks_sensitive_query_values_even_when_unregistered():
    masked = safe_url("https://partner.example.com/x?token=abc123&month=2026-09")
    assert "abc123" not in masked
    assert "month=2026-09" in masked


def test_evidence_written_to_the_repo_is_redacted():
    SECRETS.register(SECRET)
    html = f"<html><table><tr><td>{SECRET}</td></tr></table></html>"
    assert SECRET not in dump_evidence(html)


def test_write_json_refuses_an_artifact_holding_a_credential(tmp_path: Path):
    SECRETS.register(SECRET)
    target = tmp_path / "flows" / "list-invoices.flow.json"
    with pytest.raises(SecretLeakError):
        write_json(target, {"steps": [{"action": "login", "password": SECRET}]})
    assert not target.exists()


def test_secret_expansion_is_never_written_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PARTNER_SESSION", SECRET)
    portal_dir = tmp_path / "portals" / "partner"
    portal_dir.mkdir(parents=True)
    yaml_path = portal_dir / "portal.yaml"
    yaml_path.write_text(
        "apiVersion: portalc/v1\n"
        "name: partner\n"
        "baseUrl: https://partner.example.com/s/${PARTNER_SESSION}\n"
        "auth:\n"
        "  mode: form\n"
        "  secrets:\n"
        "    session: PARTNER_SESSION\n"
        "policy:\n"
        "  allowedDomains:\n"
        "    - partner.example.com\n"
    )
    cfg = PortalConfig.from_file(yaml_path)

    assert cfg.base_url.endswith(SECRET)  # expanded in memory, for this call only
    assert "${PARTNER_SESSION}" in yaml_path.read_text()  # and never on disk
    with pytest.raises(SecretLeakError):
        write_json(portal_dir / "flows" / "f.flow.json", {"baseUrl": cfg.base_url})


def test_compiled_artifacts_never_contain_the_portal_password(workspace: Path):
    from portalc.pipeline import explore, verify

    ws = Workspace(workspace)
    explore(ws, "demo-marina", "list-invoices", accept=True)
    verify(ws, "demo-marina", "list-invoices")

    password = "demo-portal-password"
    written = [p for p in workspace.rglob("*") if p.is_file() and p.suffix in {".json", ".html", ".md", ".ts"}]
    assert written
    offenders = [str(p) for p in written if password in p.read_text(errors="replace")]
    assert offenders == []


# -- lockfile integrity -------------------------------------------------------


def _locked_workspace(tmp_path: Path) -> tuple[Workspace, Path, Path]:
    ws = Workspace(tmp_path)
    flow_path = tmp_path / "portals" / "p" / "flows" / "f.flow.json"
    schema_path = tmp_path / "portals" / "p" / "schemas" / "s.schema.json"
    write_json(flow_path, {"apiVersion": "portalc/v1", "name": "f", "schema": "s", "steps": []})
    write_json(schema_path, {"title": "S", "type": "array", "items": {"properties": {"a": {"type": "string"}}}})
    record_green(ws, "p", "f", schema_name="s", version="0.1.0", flow_path=flow_path, schema_path=schema_path)
    return ws, flow_path, schema_path


def test_the_lockfile_carries_a_digest_of_itself(tmp_path: Path):
    ws, _, _ = _locked_workspace(tmp_path)
    lock = json.loads(ws.lockfile_path.read_text())
    assert lock[INTEGRITY_FIELD].startswith("sha256:")
    assert load_lock(ws)["portals"]["p"]["flows"]["f"]["schema"] == "s"


def test_a_hand_edited_lockfile_is_rejected(tmp_path: Path):
    ws, _, _ = _locked_workspace(tmp_path)
    lock = json.loads(ws.lockfile_path.read_text())
    lock["portals"]["p"]["flows"]["f"]["flowSha256"] = "0" * 64
    ws.lockfile_path.write_text(json.dumps(lock, indent=2))
    with pytest.raises(LockIntegrityError) as caught:
        load_lock(ws)
    assert "modified" in caught.value.title


def test_a_legacy_lockfile_without_a_digest_still_loads(tmp_path: Path):
    ws, flow_path, schema_path = _locked_workspace(tmp_path)
    lock = json.loads(ws.lockfile_path.read_text())
    lock.pop(INTEGRITY_FIELD)
    ws.lockfile_path.write_text(json.dumps(lock, indent=2))
    assert load_lock(ws)["portals"]["p"]
    findings = integrity_findings(ws, "p", "f", flow_path=flow_path, schema_path=schema_path)
    assert any(f.check == "lock.integrity" and f.severity == "info" for f in findings)


def test_a_flow_edited_after_the_last_green_run_is_reported(tmp_path: Path):
    ws, flow_path, schema_path = _locked_workspace(tmp_path)
    write_json(flow_path, {"apiVersion": "portalc/v1", "name": "f", "schema": "s", "steps": [{"action": "login"}]})
    findings = integrity_findings(ws, "p", "f", flow_path=flow_path, schema_path=schema_path)
    assert [f.check for f in findings if not f.ok] == ["lock.flow"]
    with pytest.raises(LockIntegrityError):
        assert_integrity(ws, "p", "f", flow_path=flow_path, schema_path=schema_path)


def test_reformatting_an_artifact_is_not_reported_as_a_change(tmp_path: Path):
    ws, flow_path, schema_path = _locked_workspace(tmp_path)
    reordered = dict(reversed(list(json.loads(schema_path.read_text()).items())))
    schema_path.write_text(json.dumps(reordered, indent=4))
    findings = assert_integrity(ws, "p", "f", flow_path=flow_path, schema_path=schema_path)
    schema_finding = next(f for f in findings if f.check == "lock.schema")
    assert schema_finding.ok
    assert "formatting" in schema_finding.message


def test_an_unlocked_flow_is_flagged_but_not_fatal(tmp_path: Path):
    ws, flow_path, schema_path = _locked_workspace(tmp_path)
    findings = integrity_findings(ws, "p", "other", flow_path=flow_path, schema_path=schema_path)
    assert [(f.check, f.severity) for f in findings] == [("lock.entry", "warn")]


def test_the_fingerprint_ignores_key_order_and_whitespace():
    assert content_fingerprint({"a": 1, "b": [1, 2]}) == content_fingerprint({"b": [1, 2], "a": 1})
    assert content_fingerprint({"a": 1}) != content_fingerprint({"a": 2})


def test_saving_the_lock_rewrites_the_digest(tmp_path: Path):
    ws, _, _ = _locked_workspace(tmp_path)
    lock = load_lock(ws)
    before = lock[INTEGRITY_FIELD]
    lock["portals"]["p"]["version"] = "0.2.0"
    save_lock(ws, lock)
    assert load_lock(ws)[INTEGRITY_FIELD] != before
